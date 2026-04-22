---
id: "20260422_s250524"
title: "Source: MiniMax-M1: Scaling Long-Context RL for Reasoning"
type: "source"
domain: "Artificial_Intelligence"
topic_cluster: "Reasoning_Models"
status: "Active"
alignment_score: 95
epistemic-status: "seed"
ttl: 365
categories: ["Artificial_Intelligence"]
tags: ["MiniMax-M1", "Long-Context", "Reinforcement_Learning", "CISPO"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/2505.24726v1.pdf"]
---

# Source: MiniMax-M1: Scaling Long-Context RL for Reasoning

## 核心内容摘要 (Abstract)
本文介绍了 **[[Entity_MiniMax-M1.md]]**，这是一个专门为长文本推理设计的模型系列。其核心贡献在于证明了通过强化学习（RL）在大规模长文本（最高 1M 上下文）上进行推理训练的可行性。MiniMax-M1 采用了 **[[Concept_Hybrid_Lightning_Attention.md]]** 架构和 **[[Concept_CISPO.md]]** 算法，在保持 Softmax 注意力推理能力的同时，极大地降低了长序列生成的算力成本。

## 关键提取 (Key Extractions)

### 技术创新
*   **CISPO (Clipped Importance Sampling Policy Optimization)**: 针对 **[[Concept_GRPO.md]]** 在长推理链中裁剪过狠的问题进行的数学改进。它允许保留低概率但具有突破性的“顿悟”Token（如 "Wait", "Aha"），这对于长程逻辑自愈至关重要。
*   **混合线性注意力 (Hybrid Lightning Attention)**: 采用 7 层线性注意力与 1 层 Softmax 注意力的交替结构，将 100K+ Token 生成的算力成本压缩至传统 Transformer 的 25%。

### 实验结论
*   **长文本 RL 的经济性**: 在 **[[Hardware_H800_GPU_Cluster.md]]** 上，MiniMax-M1 仅用 3 周时间就完成了从基础模型到具备 1M 上下文推理能力的进化。
*   **逻辑深度与长度的权衡**: 虽然线性注意力带来了极大的效率提升，但在处理 AIME 2025 等顶级复杂数学问题时，仍显示出与纯 Softmax 架构细微的精度差距[^1]。

## 与 Wiki 的联系
*   **支持 [[Concept_Agentic_AI.md]]**: 提供了将推理成本降低、长度拉伸的底层架构支持。
*   **修正 [[Concept_GRPO.md]]**: 通过 CISPO 解决了 GRPO 在信度分配（Credit Assignment）上的局部坍缩问题。

[^1]: [[Source_2505.24726v1.md]]
