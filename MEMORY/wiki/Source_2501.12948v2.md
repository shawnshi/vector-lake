---
id: "20260422_hf001"
title: "DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning"
type: "source"
domain: "Artificial_Intelligence"
topic_cluster: "Reasoning_Models"
status: "Active"
alignment_score: 98
epistemic-status: "seed"
ttl: 365
categories: ["Artificial_Intelligence"]
tags: ["DeepSeek-R1", "Reinforcement_Learning", "GRPO", "Reasoning_Distillation"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/2501.12948v2.md", "raw/Huggingface-Daily-Papers/2501.12948v2.pdf"]
---

# DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning

## 核心摘要
本论文介绍了 DeepSeek-R1 系列模型，重点展示了如何通过强化学习（RL）提升大语言模型的推理能力。DeepSeek-R1-Zero 证实了在不使用监督微调（SFT）的情况下，纯 RL 可以激发模型的自我纠错与长逻辑链推理能力。而正式版的 DeepSeek-R1 通过多阶段训练（冷启动 SFT + RL）达到了与 OpenAI o1 相当的性能，并将其推理能力蒸馏到了更小的模型中。

## 关键技术与发现
- **纯 RL 驱动推理 (Pure RL-driven Reasoning)**: 仅通过基于规则的奖励（如数学答案正确、格式合规），模型自发涌现出思考、纠错等复杂行为 [^1]。
- **GRPO (Group Relative Policy Optimization)**: 一种新型 RL 算法，通过取消独立的奖励模型来降低显存消耗并提升效率 [^1]。
- **推理能力蒸馏 (Reasoning Distillation)**: 证明了大模型的推理路径可以作为高质量 SFT 数据，使小模型（如 1.5B, 7B）获得远超其规模的逻辑能力 [^1]。
- **性能基准**: 在 AIME 2024 (79.8% Pass@1) 和 MATH-500 (97.3%) 等基准上表现出色 [^1]。

## 行业影响与联系
- **[[Concept_Agentic_AI.md]]**: R1 的长链推理是实现自主智能体在复杂医疗审计等场景下执行任务的核心逻辑引擎。
- **[[Concept_The_Bitter_Lesson.md]]**: 再次验证了算力与客观规则驱动的进化上限高于人类经验标注。
- **医疗应用**: 其高度确定的推理路径可作为 **[[Concept_MSL_医疗语义层.md]]** 的执行器，降低概率幻觉。

[^1]: [[Source_2501.12948v2.md]]
