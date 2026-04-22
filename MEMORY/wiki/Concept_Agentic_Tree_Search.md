---
id: "20240410_c_ats"
title: "Concept: Agentic Tree Search"
type: "concept"
domain: "Artificial_Intelligence"
topic_cluster: "Agentic_Reasoning"
status: "Active"
alignment_score: 95
epistemic-status: "sprouting"
ttl: 1825
categories: ["Artificial_Intelligence"]
tags: ["Search", "Reasoning", "Backtracking", "AI_Scientist"]
created: "2024-04-10"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/2504.08066v1.pdf"]
---

# Concept: Agentic Tree Search

## 定义
**Agentic Tree Search (智能体树搜索)** 是一种将决策过程建模为树状结构，并由 AI 智能体自主进行搜索、评估与回溯的算法范式。它超越了传统 LLM 的线性生成（Greedy Decoding），通过引入“后悔机制”和“多路径并行探索”，显著提升了处理复杂、长程任务的成功率。

## 核心机制
1. **分支探索 (Branching)**: 针对同一目标生成多个潜在的行动方案。
2. **自我评估 (Self-Evaluation)**: 智能体充当裁判，对各分支的成功概率进行评分。
3. **回溯 (Backtracking)**: 当某一路径被证明失效（遇到“物理摩擦”或“逻辑断层”）时，系统强制返回上一个决策节点并尝试新分支。

## 实证案例：AI Scientist v2
在 [Relation:: [[Source_2504.08066v1.md]]] 中，智能体树搜索被用于科学实验的自主规划。当编写的代码运行报错时，智能体不会盲目在当前错误上打补丁，而是会评估是否需要回溯到更早的假设节点，重新调整实验设计。这使得 AI 能够自主完成复杂的科研闭环[^1]。

## 医疗 IT 应用场景
- **精益诊疗路径模拟**: 在 [Relation:: [[Concept_Clinical_Pathway.md]]] 的优化中，利用树搜索模拟不同干预手段对床位周转率的影响。
- **咨询方案博弈**: 模拟多种数字化转型路径，寻找抗风险能力最强的架构组合。

## 关联页面
- [Supports:: [[Concept_Agentic_AI.md]]]
- [Supports:: [[Concept_Agent_Verification_Loop.md]]]

[^1]: [[Source_2504.08066v1.md]]
