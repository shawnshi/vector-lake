---
id: "20260422_s250608"
title: "Source: Reflect-Retry-Reward: Reasoning with Two-Stage Masked Reward"
type: "source"
domain: "Artificial_Intelligence"
topic_cluster: "Reinforcement_Learning"
status: "Active"
alignment_score: 92
epistemic-status: "seed"
ttl: 365
categories: ["Artificial_Intelligence"]
tags: ["RRR", "Masked_Reward", "Self-Correction", "Qwen-2"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/2506.08007v1.pdf"]
---

# Source: Reflect-Retry-Reward: Reasoning with Two-Stage Masked Reward (RRR)

## 核心内容摘要 (Abstract)
本研究提出了 **[[Concept_Reflect-Retry-Reward.md]]** (RRR) 框架，旨在解决推理模型在缺乏高质量纠错轨迹（Distillation Data）时的自愈训练问题。RRR 通过“掩码奖励（Masked Reward）”机制，仅对导致重试成功的“反思阶段”发放奖励，显著提升了模型在数学（MATH）和代码（HumanEval）任务中的自我纠错能力。

## 关键提取 (Key Extractions)

### 技术核心
*   **两阶段掩码奖励 (Two-Stage Masked Reward)**: 传统的 RL往往对整个轨迹进行奖励，导致信度分配模糊。RRR 强制将奖励锚定在反思（Reflect）和重试（Retry）Token 上。
*   **去蒸馏化 (De-distillation)**: 实验证明，即便没有 GPT-4 提供的黄金纠错路径，仅凭二元结果反馈（对/错），也能训练出性能超越大模型 10 倍规模的纠错能力[^1]。

### 实验观察
*   **模型起步阈值**: 在 **[[Entity_Llama_3.2-3B.md]]** 上效果有限，表明模型需要具备一定的基础推理“火花”才能通过 RRR 实现自启。
*   **Qwen-2 增益**: **[[Entity_Qwen_2.md]]** 在挂载 RRR 后，其纠错成功率在 MATH 任务上提升了 15% 以上。

## 与 Wiki 的联系
*   **深化 [[Concept_Agent_Verification_Loop.md]]**: 提供了该循环在算法训练层面的精确数学实现。
*   **对冲 [[Concept_Probability_Machine.md]]**: 通过确定性的结果反馈，将概率生成的轨迹强制收敛于正确的逻辑闭环。

[^1]: [[Source_2506.08007v1.md]]
