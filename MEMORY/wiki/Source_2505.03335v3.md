---
id: "20260422_s03335"
title: "Source: AbsoluteZero: Towards Code-based Self-Play for Reasoning"
type: "source"
domain: "Artificial_Intelligence"
topic_cluster: "Reasoning_Evolution"
status: "Active"
alignment_score: 88
epistemic-status: "seed"
ttl: 365
categories: ["Artificial_Intelligence"]
tags: ["AbsoluteZero", "Self-Play", "Reasoning", "Reinforcement_Learning", "ByteDance"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/2505.03335v3.md", "raw/Huggingface-Daily-Papers/2505.03335v3.pdf"]
---

# AbsoluteZero: Towards Code-based Self-Play for Reasoning

## 核心论点：推理进化的“人类脱钩”
**AbsoluteZero** 提出了一个极具挑衅性的范式：利用代码执行器作为“接地环境 (Grounded Environment)”，实现逻辑推理能力的自我进化。该框架证明，模型可以在没有任何人类标注数据的前提下，通过自对弈 (Self-Play) 进化出超越人类标注水平的推理能力[^1]。

## 运行机制：基于代码的自对弈
1. **多样化任务生成**: 自动生成演绎（推输出）、溯理（找输入）和归纳（写代码）任务。
2. **代码执行反馈**: 将代码运行结果作为唯一的物理真理。执行成功则奖励，失败则通过 [Relation:: [[Concept_Agent_Verification_Loop.md]]] 进行回溯修正。
3. **强化学习优化**: 利用 PPO 或 GRPO 算法持续迭代模型权重。

## 行业启示
- **数据债务的终结**: 解决了 [Relation:: [[Concept_Data_Debt.md]]] 问题。在高质量医疗标注数据稀缺的情况下，通过“医疗逻辑代码化”进行模拟训练成为可能。
- **算力换智能**: 采样算力将取代数据量，成为推理能力的新瓶颈。
- **接地性 (Groundedness)**: 强调了“物理反馈”在消除 AI 幻觉中的核心作用。

[^1]: [[Source_2505.03335v3.md]]
