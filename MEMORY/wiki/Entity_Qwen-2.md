---
id: "20260422_e_qwen2"
title: "Entity: Qwen-2"
type: "entity"
domain: "Artificial_Intelligence"
topic_cluster: "Foundation_Models"
status: "Active"
alignment_score: 85
epistemic-status: "sprouting"
ttl: 1095
categories: ["Artificial_Intelligence"]
tags: ["Alibaba", "Qwen", "Base_Model", "Open_Source"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/2506.08007v1.pdf", "raw/Huggingface-Daily-Papers/2506.13585v1.pdf"]
---

# Entity: Qwen-2

## 定义与背景
**Qwen-2** 是阿里巴巴 Qwen 团队发布的开源大语言模型系列，包含从 0.5B 到 72B 等多种规模。在 2025-2026 年的推理模型研究中，Qwen-2-7B 和 Qwen-2-72B 常被作为强化学习训练的基础模型（Base Model）。

## 关键实践
*   **强化预训练 (RPT)**: 在 **[[Source_2506.13585v1.md]]** 中，Qwen-2 被用于验证强化学习预训练范式，证明了原始语料可作为奖励源。
*   **自我纠错 (RRR)**: 在 **[[Source_2506.08007v1.md]]** 中，通过 Reflect-Retry-Reward 框架，Qwen-2-7B 展示了在没有蒸馏数据的情况下显著提升数学推理纠错率的能力[^1]。

## 与 Wiki 的联系
*   **属于:: [[Entity_Alibaba_Cloud.md]]**
*   **对标:: [[Entity_Llama_3.2.md]]**
*   **后续:: [[Entity_Qwen3.md]]**

[^1]: [[Source_2506.08007v1.md]]
