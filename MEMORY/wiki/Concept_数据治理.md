---
id: "20260410_dg001"
title: "Concept_数据治理"
type: "concept"
domain: "Medical_IT"
topic_cluster: "Data_Strategy"
status: "Active"
alignment_score: 100
epistemic-status: "evergreen"
ttl: 1825
categories: ["Healthcare_IT"]
tags: ["数据质量", "标准化", "OMOP", "数据资产"]
created: "2026-04-10"
updated: "2026-04-22"
sources: ["raw/article/医疗数字化三十讲/第二十讲：大数据：从“杂乱无章”到“价值连城”的点金术.md"]
---

# Concept_数据治理

## 定义
医疗数据治理是确保医疗机构数据资产具备真实性 (Veracity)、一致性 (Consistency) 和可用性 (Usability) 的全生命周期管理活动。它是所有医疗 AI 与大数据分析的底层“地基工程”[^1]。

## 核心支柱 (三位一体比喻)
1.  **数据宪法 (Standards)**：定义元数据、主数据及业务代码的标准。强制遵循 **[[Concept_OMOP_CDM.md]]** 或国家互联互通标准[^1]。
2.  **质检站 (Quality)**：建立自动化的数据质控引擎（DQE），实时监测数据缺失、逻辑矛盾与异常值。
3.  **身份证 (Metadata)**：实现数据的血缘追踪与全生命周期确权[^1]。
4.  **数据结构化 (Data Structuring)**：将非结构化文本转化为机器可读的“活数据”。这是从信息化迈向数字化的关键跳跃，是 AI 应用与科研决策的基础 [支持:: [[Source_第六讲：心脏与大脑（上）：以电子病历（EMR）为核心的临床数据体系.md]]]。
5.  **数据治理“市政工程”**: 将 **[[Entity_运营数据中心_ODR.md]]** 的构建比作城市规划与市政基建。涉及主数据管理 (MDM) 解决同名异物问题、ETL 清洗流程以及面向决策的主题库建设。它是长周期的底层基建，决定了上层决策应用的稳固性 [衍生于:: [[Source_第十三讲：数据决策之脑：医院运营数据中心（ODR）与 BI 应用.md]]]。
6.  **偿还债务 (Debt Repayment)**: 数据治理的本质是偿还历史积累的技术债务与管理债务。在 LLM 时代，只有通过深度治理将“数据负债”转化为“数据资产”，才能建立有效的算法护城河 [支持:: [[Concept_Data_Balance_Sheet.md]]][^2].


## 战略挑战
- **理还乱 (Complexity)**：医疗数据的高维、非标与异构。
- **重建设轻治理**：医院普遍倾向于采购前端应用，而忽视底层数据的深度清洗与标准化。
- **制度深潜**：数据治理需要跨部门的流程再造，往往触及既有的权力结构。

## 关联关系
- [支持:: [[Concept_MSL_医疗语义层.md]]]
- [支持:: [[Concept_可信数据空间.md]]]
- [属于:: [[Entity_WiNEX.md]]] (WiNEX DnA 平台的核心任务)
- [支持:: [[Concept_Data_Balance_Sheet.md]]]

[^1]: [[Source_第二十讲：大数据：从“杂乱无章”到“价值连城”的点金术.md]]
[^2]: [[Source_第四讲：数据资产负债表 —— LLM的“燃料”与“负债”.md]]
