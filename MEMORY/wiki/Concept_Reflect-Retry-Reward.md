---
id: "20260422_c_rrr"
title: "Concept: Reflect-Retry-Reward (RRR)"
type: "concept"
domain: "Artificial_Intelligence"
topic_cluster: "Reinforcement_Learning"
status: "Active"
alignment_score: 94
epistemic-status: "seed"
ttl: 1825
categories: ["Artificial_Intelligence"]
tags: ["Self-Correction", "RL", "Masked_Reward", "Thinking_Models"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/2506.08007v1.pdf"]
---

# Concept: Reflect-Retry-Reward (RRR)

## 定义
**Reflect-Retry-Reward (RRR)** 是一种旨在提升推理模型自我纠错（Self-Correction）能力的强化学习框架。其核心理念是：模型不需要依赖外部高质量的纠错数据，只需要通过二元的结果反馈（Reward）和针对性的 **[[Concept_Masked_Reward.md]]** 就能实现自愈进化。

## 运作流程
1.  **Reflect**: 模型检测到输出错误或逻辑矛盾。
2.  **Retry**: 模型基于反思生成新的推理尝试。
3.  **Reward**: 如果重试成功，通过掩码机制仅对 Reflect 和 Retry 部分进行奖励，忽略原始错误路径。

## 关键洞察
RRR 证明了自愈能力可以从二元反馈中“涌现”。这为 **[[Concept_Agent_Verification_Loop.md]]** 提供了算法层的实现：让“概率机器”在不断的失败与成功中，通过奖励信号固化下“反思”这一逻辑动作。

## 关联关系
*   **支持:: [[Concept_Agent_Verification_Loop.md]]**
*   **关联:: [[Concept_Masked_Reward.md]]**
*   **实验对象:: [[Entity_Qwen-2.md]]**

[^1]: [[Source_2506.08007v1.md]]
