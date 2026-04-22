---
id: "20260422_s09388"
title: "Source: Qwen3: Reasoning with Thinking Models at Scale"
type: "source"
domain: "Artificial_Intelligence"
topic_cluster: "Reasoning_Models"
status: "Active"
alignment_score: 100
epistemic-status: "seed"
ttl: 365
categories: ["Artificial_Intelligence"]
tags: ["Qwen3", "Thinking_Mode", "Logit_Distillation", "Reasoning_Models"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/2505.09388v1.md", "raw/Huggingface-Daily-Papers/2505.09388v1.pdf"]
---

# Qwen3: Reasoning with Thinking Models at Scale

## 核心论点
阿里巴巴 Qwen3 团队提出了一套完整的推理模型体系。其核心贡献在于证明了通过 **[[Concept_Reasoning_Distillation.md]]**（特别是 Logit 蒸馏），可以将顶级推理模型（如 o1 级别）的逻辑路径有效地“移植”到小参数模型中，实现推理能力的平民化与边缘化。

## 关键技术发现
1. **Thinking Mode Fusion (思维模式融合)**: 在单一模型中集成了快速响应（System 1）与深度推理（System 2）模式。用户可通过 `/think` 标签显式调用推理能力 [^1]。
2. **Logit 蒸馏 (Logit Distillation)**: 相比于传统的文本蒸馏，Logit 蒸馏能捕捉到 Teacher 模型在推理过程中的概率分布，使 Student 模型在 3B/8B 规模下也能展现出惊人的逻辑一致性 [^1]。
3. **Thinking Budget (思维预算)**: 引入了对推理 Token 数的物理限制，确保模型在复杂任务中不会陷入无限循环，同时也为实时应用提供了确定性的耗时保障 [^1]。

## 行业影响
Qwen3 的出现标志着推理模型进入“普惠时代”。对于医疗 IT 而言，这意味着 **[[Concept_Cerebellarization_1bit_LLM.md]]** 架构有了更强大的底层算法支撑，使得端侧设备具备了处理复杂临床逻辑的可能性。

[^1]: [[Source_2505.09388v1.md]]
