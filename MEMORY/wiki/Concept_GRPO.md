---
id: "20260207_grpo"
title: "Concept: GRPO (Group Relative Policy Optimization)"
type: "concept"
domain: "Artificial_Intelligence"
topic_cluster: "Reinforcement_Learning"
status: "Active"
alignment_score: 95
epistemic-status: "sprouting"
ttl: 1825
categories: ["Artificial_Intelligence"]
tags: ["RL", "Efficiency", "DeepSeek", "CISPO"]
created: "2026-02-07"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/2505.24726v1.pdf"]
---

# Concept: GRPO (Group Relative Policy Optimization)

## 定义
**GRPO** 是一种旨在降低强化学习算力成本、移除传统批评者（Critic）网络的优化算法。它通过组内相对奖励来代替绝对状态价值评估，是推理模型（如 DeepSeek-R1）大规模 RL 训练的核心。

## 缺陷与补丁 (2026 更新)
*   **信度分配瓶颈**: 实验发现 GRPO 在处理极长推理链（1M+ 上下文）时，其重要性采样（Importance Sampling）的裁剪机制会导致低概率但高价值的推理 Token（顿悟时刻）被抑制。
*   **CISPO 的对冲**: 为了解决上述问题，**[[Concept_CISPO.md]]** 算法在 GRPO 的基础上进行了数学修正，旨在保留长程推理中的关键逻辑点[^1]。

## 关联关系
*   **对比:: [[Concept_CISPO.md]]**
*   **支持:: [[Entity_DeepSeek_R1.md]]**

[^1]: [[Source_2505.24726v1.md]]
