---
id: "20260422_c_masked_reward"
title: "Concept: Masked Reward"
type: "concept"
domain: "Artificial_Intelligence"
topic_cluster: "Reinforcement_Learning"
status: "Active"
alignment_score: 96
epistemic-status: "seed"
ttl: 1825
categories: ["Artificial_Intelligence"]
tags: ["RL", "Credit_Assignment", "RRR", "Masking"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/2506.08007v1.pdf"]
---

# Concept: Masked Reward (掩码奖励)

## 定义
**掩码奖励 (Masked Reward)** 是一种精细化的强化学习信度分配（Credit Assignment）技术。它通过掩盖掉那些不相关的推理片段，仅将奖励信号作用于关键的动作（Action）或思维转变（Thought Shift）上。

## 核心应用：RRR 框架
在 **[[Concept_Reflect-Retry-Reward.md]]** (RRR) 框架中，掩码奖励机制被用于自我纠错训练：
*   **忽略正确路径**: 如果模型第一次就做对了，该路径不作为纠错训练的重点。
*   **锁定反思点**: 当模型在反思（Reflect）后重试（Retry）成功，奖励被精准地分配给“意识到错误”和“执行修正”的 Token 序列[^1]。

## 战略价值
掩码奖励解决了长链条推理中“结果正确但不代表过程逻辑完美”的欺骗性问题。它是构建 **[[Concept_Agent_Verification_Loop.md]]** 的核心数学组件，确保模型在“为了奖励而走捷径”与“真实逻辑闭环”之间选择后者。

## 关联页面
*   **属于:: [[Concept_Reflect-Retry-Reward.md]]**
*   **支持:: [[Concept_Agent_Verification_Loop.md]]**

[^1]: [[Source_2506.08007v1.md]]
