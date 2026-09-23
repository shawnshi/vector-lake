"""A claim's memory slot is what the claim *says* it is, never what its prose resembles.

The keyword scan this replaces produced 6,681 records across two slots that were entirely
fabricated: every ``decision`` record was a page section whose text contained 方案/采用/决策, and
every ``task_state`` record was one containing 状态.  These tests hold the door shut, using text
shaped like the records that were found in the store.
"""

import pytest

from vector_lake import governance_store


@pytest.mark.parametrize(
    "text",
    [
        "本页给出的实施路线是一个可落地方案，建议采用分阶段部署的建设模式。",
        "该院已完成电子病历六级评审，系统状态为运行中。",
        "# Agent Workflow-Context 四象限 ## 1. 编译事实 (Compiled Truth - READ MODEL)",
        "# Attention Collapse ## 2. 证据时间线 (Timeline - EVENT STORE)",
        "用户在归档时偏好按年度保存原始文件。",
        "决策含义：本项目决定采用国产化替代路线。",
        "Review & 待办建议：下一轮需要补齐缺口。",
    ],
)
def test_prose_that_resembles_a_slot_does_not_fill_it(text):
    claim = {"claim_text": text, "source_page": "Concept_某分析页.md"}
    assert governance_store.infer_memory_type(claim) == "fact"


def test_an_explicit_memory_type_wins_over_the_prose():
    claim = {
        "memory_type": "preference",
        "claim_text": "该方案决定采用一次性全量迁移，状态为进行中。",
    }
    assert governance_store.infer_memory_type(claim) == "preference"


def test_a_claim_type_that_names_a_slot_is_honoured():
    claim = {"claim_type": "task_state", "claim_text": "无关键词的句子。"}
    assert governance_store.infer_memory_type(claim) == "task_state"


def test_a_hyphenated_declaration_is_normalised():
    claim = {"memory_type": "Task-State", "claim_text": "任意文本。"}
    assert governance_store.infer_memory_type(claim) == "task_state"


def test_an_unknown_declaration_falls_through_to_fact():
    # A slot that does not exist must not become a slot by being spelled confidently.
    claim = {"memory_type": "insight", "claim_text": "任意文本。"}
    assert governance_store.infer_memory_type(claim) == "fact"


def test_a_bare_claim_is_a_fact():
    assert governance_store.infer_memory_type({"claim_text": "试点医院上线了新的集成平台。"}) == "fact"
