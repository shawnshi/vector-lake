---
id: "20260422_e044mu"
title: "Entity: MUSE (Multi-LLM Uncertainty via Subset Ensembles)"
type: "entity"
domain: "Artificial_Intelligence"
topic_cluster: "Safety_Framework"
status: "Active"
alignment_score: 98
epistemic-status: "evergreen"
ttl: 1095
categories: ["Artificial_Intelligence"]
tags: ["MUSE", "不确定性量化", "LLM安全", "子集集成"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/DigitalHealthLecturesScout/DH-Lectures-20260406.md"]
---

# Entity: MUSE (Multi-LLM Uncertainty via Subset Ensembles)

## 概述
MUSE 是一种用于量化大语言模型（LLM）输出**不确定性**的技术框架。它通过子集集成（Subset Ensembles）的方法，为 AI 的每一个结论提供置信区间分数。

## 临床价值
在医疗 AI 应用中，MUSE 解决了“概率机器”给临床带来的风险。当置信度低于特定阈值时，系统会触发强制性人工审核，是实现 **[[Concept_幻觉熔断机制.md]]** 的关键技术组件[^1]。

## 关联页面
- [属于:: [[Concept_MUSE_不确定性量化.md]]]
- [支持:: [[Source_DH-Lectures-20260406.md]]]

[^1]: [[Source_DH-Lectures-20260406.md]]
