---
id: "20260422_c006"
title: "Concept: ACE (智能体协作引擎 / Agentic Collaboration Engine)"
type: "concept"
domain: "Medical_IT"
topic_cluster: "Artificial_Intelligence"
status: "Active"
alignment_score: 100
epistemic-status: "sprouting"
ttl: 1825
categories: ["Artificial_Intelligence"]
tags: ["多智能体", "调度引擎", "卫宁健康", "T2A", "合规编排"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/digitalhealthobserve/卫宁健康：在 Agentic AI 时代的竞争壁垒重构_2026-02-12.md", "raw/DigitalHealthLecturesScout/Weekly_DigitalHealth_20260329.md", "raw/DigitalHealthWeeklyBrief/DHWB-20260315.md", "raw/DigitalHealthWeeklyBrief/DHWB-20260322.md"]
---

# Concept: ACE (智能体协作引擎)

## 定义
ACE 是 **[[Entity_卫宁健康.md]]** Agentic AI 架构中的核心指挥中枢。它负责协调多个专病、专岗智能体（Agents）之间的通讯、任务分配与合规编排，是实现 [支持:: [[Concept_T2A_Text-to-Action]]] 的关键执行底座。

## 核心职责
- **任务分解**: 将复杂的临床意图分解为可执行的子任务。
- **冲突裁决**: 当不同智能体给出的建议冲突时，依据医学权威指南执行逻辑仲裁。
- **合规编排**: 确保所有 Agent 的调用符合预设的安全策略和临床约束，被定义为数智化转型的交付标准[^3]。
- **治理与审计**: 强制集成 **[[Entity_Stanford_NOHARM.md]]** 标准的实时反向检索与“认知摩擦”机制，防止自主执行逻辑逃逸[^4]。

## 行业趋势 (2026)
随着医疗机构 Agentic AI 预算占比跃升至 **29%**，ACE 作为多智能体调度的“交警”和“质控员”，其战略地位已超越单一算法模型[^2]。

## 关联
- [属于:: [[Concept_Agentic_Hospital.md]]]
- [支持:: [[Concept_T2A_Text-to-Action.md]]]
- [调用:: [[Concept_MSL_医疗语义层.md]]]
- [防御:: [[Concept_Death_by_AI.md]]]

[^1]: [[Source_卫宁健康：在 Agentic AI 时代的竞争壁垒重构_2026-02-12.md]]
[^2]: [[Source_Weekly_DigitalHealth_20260329.md]]
[^3]: [[Source_DHWB-20260315.md]]
[^4]: [[Source_DHWB-20260322.md]]
