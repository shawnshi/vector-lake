---
id: "20260422_c_think_budget"
title: "Concept: 思维预算 (Thinking Budget)"
type: "concept"
domain: "Artificial_Intelligence"
topic_cluster: "Resource_Management"
status: "Active"
alignment_score: 98
epistemic-status: "seed"
ttl: 1825
categories: ["Artificial_Intelligence", "System_Architecture"]
tags: ["Token_Limit", "Anytime_Algorithm", "Efficiency", "Predictability"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/2505.09388v1.md"]
---

# Concept: 思维预算 (Thinking Budget)

## 核心定义
思维预算是指对大语言模型在执行推理任务时所能消耗的逻辑 Token 数、计算时间或电力成本进行的物理限制。

## 核心特征
1. **Anytime 属性**: 借鉴了 Anytime Algorithms 的思想，模型应能在思维预算耗尽时，根据已有的思考过程交付一个“当前最佳”结论。
2. **确定性保障**: 解决了复杂推理模型在实时生产环境（如临床手术辅助、急诊预检）中响应时间不可控的风险。
3. **成本围栏**: 防止模型在陷入逻辑死循环时导致算力资源的无限损耗。

## 医疗防护应用
- **[[Concept_MSL_医疗语义层.md]] 的物理层**: 可作为 MSL 的刚性约束，强制 AI 在预定时间内完成合规性校验。
- **分级分诊策略**: 简单任务分配低预算，复杂疑难杂症分配高预算，实现医院算力资源的高效调度。

[^1]: [[Source_2505.09388v1.md]]
