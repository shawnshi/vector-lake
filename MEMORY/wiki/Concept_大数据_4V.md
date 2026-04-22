---
id: "20260422_con_4v"
title: "大数据 4V"
type: "concept"
domain: "System_Architecture"
topic_cluster: "General"
status: "Active"
alignment_score: 90
epistemic-status: "evergreen"
ttl: 1825
categories: ["System_Architecture"]
tags: ["大数据", "数据特征", "4V理论"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗数字化三十讲/第二十讲：大数据：从“杂乱无章”到“价值连城”的点金术.md"]
---

# 医疗大数据 4V 特征

## 维度解析
医疗大数据的价值挖掘受限于其独特的 4V 属性[^1]：
1. **Volume (体量)**：影像数据、组学数据呈爆炸式增长。
2. **Velocity (速率)**：实时监护与可穿戴设备产生的流数据。
3. **Variety (多样性)**：结构化 (检验)、半结构化 (报告)、非结构化 (影像、文本) 并存。
4. **Veracity (真实性)**：**医疗领域的核心挑战**。医生书写偏差、标准不一导致“数据水质”堪忧。

## 应对策略
- 通过 **[[Concept_数据治理]]** 提升 Veracity。
- 利用 **[[Entity_OMOP_CDM]]** 规范 Variety。

[^1]: [[Source_第二十讲：大数据：从“杂乱无章”到“价值连城”的点金术.md]]
