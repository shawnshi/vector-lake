---
id: "20260422_s07143"
title: "Source: Leave No Context Behind: Efficient Infinite Context LLMs with Infini-attention"
type: "source"
domain: "Artificial_Intelligence"
topic_cluster: "LLM_Architecture"
status: "Active"
alignment_score: 95
epistemic-status: "seed"
ttl: 365
categories: ["Artificial_Intelligence"]
tags: ["Infini-attention", "Infinite_Context", "Compressive_Memory", "Google"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/2404.07143v2.pdf"]
---

# Leave No Context Behind: Efficient Infinite Context LLMs with Infini-attention

## 概览 (Overview)
Google 研究团队提出的一种新颖注意力机制 **Infini-attention**，旨在使大语言模型（LLM）能够在有限的计算资源和内存下处理无限长度的上下文。该技术通过在标准注意力机制中集成 **压缩记忆 (Compressive Memory)**，实现了对长程信息的有效保留。

## 核心技术与架构 (Core Technology)
- **Infini-attention 结构**: 结合了局部注意力（Standard Masked Attention）和全局注意力（Linear Compressive Memory）。
- **三层逻辑**:
    1. **局部注意力**: 处理当前序列块的精确细节。
    2. **压缩记忆**: 使用线性相关矩阵存储历史块的抽象表征。
    3. **门控融合 (Gated Fusion)**: 动态平衡局部细节与长程背景的权重。
- **增量更新 (Delta Rule)**: 优化了记忆更新算法，通过减去旧有的检索值来提高联想记忆的精度。

## 关键发现 (Key Findings)
- **114倍压缩比**: 在 1M 序列长度的任务中，相比标准 Transformer，Infini-attention 实现了极高的内存压缩比。
- **常数级内存占用**: 无论输入序列多长，KV Cache 的大小保持固定，解决了长文本预测中的算力黑洞问题。
- **微调可行性**: 证明了现有的预训练模型可以通过持续预训练（Continual Pre-training）快速获得无限上下文处理能力。

## 对知识库的意义 (Significance)
- **[[Concept_Infini_attention]]**: 提供了物理实现路径，支撑了“医疗长卷病历处理”的低成本化。
- **[[Concept_Cerebellarization_1bit_LLM]]**: 进一步强化了边缘端处理复杂长程逻辑的可能性。

[^1]: [[Source_2404.07143v2.md]]
