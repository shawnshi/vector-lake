---
id: "20260422_c047uq"
title: "Concept: MUSE 不确定性量化"
type: "concept"
domain: "Artificial_Intelligence"
topic_cluster: "Safety_Framework"
status: "Active"
alignment_score: 98
epistemic-status: "evergreen"
categories: ["Artificial_Intelligence"]
tags: ["不确定性", "置信度", "临床决策支持", "MUSE"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/DigitalHealthLecturesScout/DH-Lectures-20260406.md"]
---

# Concept: MUSE 不确定性量化

## 定义
指医疗 AI 系统不仅输出临床建议，还必须通过 **[[Entity_MUSE.md]]** 等技术手段，同步输出该建议的**置信区间分数**（Confidence Score）。

## 应用逻辑
1. **认知摩擦引入**：当分数低于 85% 时，系统强制要求人工干预 [支持:: [[Concept_Cognitive_Friction.md]]]。
2. **幻觉熔断**：分数低于 50% 时，系统自动撤回输出，防止错误引导临床动作。

## 关联页面
- [关联:: [[Concept_幻觉熔断机制.md]]]
- [支持:: [[Source_DH-Lectures-20260406.md]]]
