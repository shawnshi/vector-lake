"""The entity-name tier: what counts as the query naming a page, and what the tier does.

The tier exists because nine query shapes on three subjects showed the same thing: a bare entity
name returns its page first (1.000), and adding the framework words an analysis question uses
("核心价值、优势及市场竞争力") drops it to rank 2, 5, 6 or 13 -- because those words appear in the
*documents that analyse the entity*, not on the entity's own page.

It is off by default, and the registered comparison says why: on the r3 label sets it lifts nDCG@5
by +0.027 (CI [+0.005, +0.051]) and MRR by +0.055, but fails the pre-registered minimum effect size
(+0.143 SD against +0.30 SD) and regresses recall, so `RULE NOT MET: keep the default`.  These tests
pin the mechanism, not the adoption.
"""

from __future__ import annotations

import pytest

from vector_lake import tool_search


def _candidate(key: str, **node) -> tuple[float, dict]:
    return (0.5, {"_key": key, **node})


def test_a_declared_title_names_the_page(monkeypatch):
    """A Chinese personal name is two characters; refusing those dropped 师成 to rank 3."""
    candidates = [_candidate("Person_Shawn-Shi", title="师成", type="Person")]

    positions = tool_search._entity_name_positions("师成关于医疗人工智能的观点", candidates)

    assert positions == {"Person_Shawn-Shi": 0}


def test_the_node_name_names_the_page_without_an_alias():
    """``Product_WiNEX`` is named by "WiNEX" even if the page declares no such alias."""
    candidates = [_candidate("Product_WiNEX", title="Product_WiNEX", type="Product")]

    positions = tool_search._entity_name_positions("WiNEX 的核心价值、优势及市场竞争力", candidates)

    assert positions == {"Product_WiNEX": 0}


def test_an_alias_names_the_page_and_the_earliest_mention_wins():
    candidates = [
        _candidate("Vendor_卫宁健康", title="Vendor_WiNing", aliases=["卫宁健康", "Winning Health"]),
        _candidate("Concept_数据中台", title="数据中台"),
    ]

    positions = tool_search._entity_name_positions("卫宁健康 的 数据中台 建设", candidates)

    assert positions["Vendor_卫宁健康"] == 0
    assert positions["Concept_数据中台"] > positions["Vendor_卫宁健康"], (
        "the subject is asked about first; a second named entity must not outrank it"
    )


def test_a_page_the_query_does_not_name_is_absent():
    candidates = [_candidate("Concept_医保通用名", title="医保通用名")]
    assert tool_search._entity_name_positions("医院数据平台 建设", candidates) == {}


def test_a_single_character_name_is_not_an_entity_name():
    """One-character fragments are what a tokenizer produces from framework words."""
    candidates = [_candidate("Concept_云", title="云")]
    assert tool_search._entity_name_positions("云的架构", candidates) == {}


def _order(monkeypatch, priority: str):
    monkeypatch.setenv("VECTOR_LAKE_ENTITY_NAME_PRIORITY", priority)
    scored = [
        (0.9, {"_key": "Source_某分析报告", "title": "某分析报告", "type": "source"}),
        (0.4, {"_key": "Vendor_卫宁健康", "title": "卫宁健康", "type": "vendor"}),
        (0.2, {"_key": "Concept_其他", "title": "其他", "type": "concept"}),
    ]
    return [node["_key"] for _, node in tool_search._entity_first_order("卫宁健康 的公司战略", scored)]


def test_the_default_order_is_untouched(monkeypatch):
    """Off by default: the registered replays were measured against this ordering."""
    assert _order(monkeypatch, "0") == ["Source_某分析报告", "Vendor_卫宁健康", "Concept_其他"]


def test_the_tier_puts_the_named_page_first_and_keeps_score_order_within_a_tier(monkeypatch):
    assert _order(monkeypatch, "1") == ["Vendor_卫宁健康", "Source_某分析报告", "Concept_其他"]


def test_the_priority_is_read_from_the_environment_and_clamped(monkeypatch):
    monkeypatch.delenv("VECTOR_LAKE_ENTITY_NAME_PRIORITY", raising=False)
    assert tool_search._entity_name_priority() == tool_search.ENTITY_NAME_PRIORITY_DEFAULT
    monkeypatch.setenv("VECTOR_LAKE_ENTITY_NAME_PRIORITY", "1")
    assert tool_search._entity_name_priority() == 1.0
    monkeypatch.setenv("VECTOR_LAKE_ENTITY_NAME_PRIORITY", "nonsense")
    assert tool_search._entity_name_priority() == tool_search.ENTITY_NAME_PRIORITY_DEFAULT


def test_the_replay_records_the_switch_so_two_runs_can_be_compared():
    """Without it in the config, ``--compare`` refuses the pair as one configuration.

    Which is the honest reading -- and it also means two runs with different ranking behaviour
    would otherwise be indistinguishable in the record.
    """
    source = (tool_search.__file__).replace("tool_search.py", "")  # noqa: F841 - clarity only
    import pathlib

    replay = pathlib.Path(__file__).resolve().parents[1] / "benchmarks" / "search_replay.py"
    assert '"entity_name_priority"' in replay.read_text(encoding="utf-8")


@pytest.mark.parametrize("raw,expected", [(" 师成 ", "师成"), ("WiNEX!", "winex"), ("A B-C", "abc")])
def test_normalisation_ignores_case_space_and_punctuation(raw, expected):
    assert tool_search._normalize_entity_name(raw) == expected
