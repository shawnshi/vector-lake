---
id: "20260422_c004dc"
title: "Decision Correction Stream (决策修正流)"
type: "concept"
domain: "Medical_IT"
topic_cluster: "Logic_Assets"
status: "Active"
alignment_score: 100
epistemic-status: "seed"
categories: ["Healthcare_IT", "System_Architecture"]
tags: ["决策修正流", "逻辑湖", "专家智慧", "数据要素"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/research/AI 原生医院的逻辑主权与 Evidence-Mesh 架构_20260310_final.md"]
---

# Decision Correction Stream (决策修正流)

## 定义
**Decision Correction Stream (决策修正流)** 是指临床专家对 AI 建议进行修正的过程数据。它记录了“AI 建议 - 专家反馈 - 最终决策”的完整闭环，反映了专家在处理复杂、边界病例时的隐性智慧[^1]。

## 核心价值
- **最高质量语料**：修正流是训练专科 Agent 的唯一高质量、带标签的语料，解决了通用模型缺乏临床深度的问题[^1]。
- **逻辑湖核心资产**：这些动态的决策数据被存储在 [[Concept_Logic_Lake.md]] 中，是医院数字化复利的最直接体现[^1]。

## 应用
- **自愈式进化**：通过分析修正流，AI 系统可以识别自身的认知偏差并进行自动微调或策略更新。
- **资产评估**：作为医疗数据要素市场中价值最高、最难获取的数据资产进行交易或入表。

[^1]: [[Source_AI 原生医院的逻辑主权与 Evidence-Mesh 架构_20260310_final.md]]
