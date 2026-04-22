---
id: "20260422_c_cispo"
title: "Concept: CISPO (Clipped Importance Sampling Policy Optimization)"
type: "concept"
domain: "Artificial_Intelligence"
topic_cluster: "Reinforcement_Learning"
status: "Active"
alignment_score: 98
epistemic-status: "seed"
ttl: 1825
categories: ["Artificial_Intelligence"]
tags: ["RL", "Optimization", "CISPO", "GRPO", "Long-CoT"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/2505.24726v1.pdf"]
---

# Concept: CISPO (Clipped Importance Sampling Policy Optimization)

## 定义
**CISPO** 是一种针对长推理链优化的强化学习算法，是对 **[[Concept_GRPO.md]]** 的直接数学演进。它旨在解决 GRPO 在处理极长序列（1M+）时，由于重要性采样权重裁剪过狠而导致的关键推理 Token（如顿悟时刻）被忽略的问题。

## 核心机制
*   **裁剪策略优化**: 相比于 GRPO 的全局粗暴裁剪，CISPO 采用了更精细的权重调整，允许模型在长推理过程中保留那些低概率但高价值的动作。
*   **信度分配增强**: 在长文本环境下，CISPO 能够更精准地识别出哪一部分推理导致了最终结果的正确，特别适用于 **[[Entity_MiniMax-M1.md]]** 这种长文本模型。

## 与 GRPO 的对比
| 特性 | GRPO | CISPO |
| :--- | :--- | :--- |
| **主要应用** | 推理模型（如 DeepSeek-R1） | 长文本推理模型（如 MiniMax-M1） |
| **权重裁剪** | 静态/全局 | 动态/重要性采样敏感 |
| **顿悟捕捉** | 容易由于概率低而被剪掉 | 受到数学保护，保留 "Wait", "Aha" 等 Token |

## 关联页面
*   **修正:: [[Concept_GRPO.md]]**
*   **支持:: [[Entity_MiniMax-M1.md]]**

[^1]: [[Source_2505.24726v1.md]]
