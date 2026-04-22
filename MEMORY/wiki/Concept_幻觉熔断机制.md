---
id: "20260422_c049hb"
title: "Concept: 幻觉熔断机制 (Hallucination Circuit Breaker)"
type: "concept"
domain: "System_Architecture"
topic_cluster: "Safety_Protocol"
status: "Active"
alignment_score: 100
epistemic-status: "evergreen"
categories: ["Artificial_Intelligence", "System_Architecture"]
tags: ["熔断", "幻觉", "MSL", "确定性"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/DigitalHealthLecturesScout/DHLS-20260419.md"]
---

# Concept: 幻觉熔断机制 (Hallucination Circuit Breaker)

## 定义
在 Agentic AI 架构中，通过 **[[Concept_MSL_医疗语义层.md]]** 建立的物理拦截机制。当大模型的输出概率分布或逻辑逻辑偏离预定义的临床真理或医保规约时，系统自动切断执行链条，防止由于幻觉引发的临床事故[^1]。

## 实现路径
- **语义比对**：将大模型输出与本地知识库（如 Vector Lake）进行毫秒级冲突检测。
- **不确定性阀值**：结合 **[[Entity_MUSE.md]]** 的分数决定是否放行。

## 关联页面
- [依赖于:: [[Concept_MSL_医疗语义层.md]]]
- [支持:: [[Concept_MUSE_不确定性量化.md]]]

[^1]: [[Source_DHLS-20260419.md]]
