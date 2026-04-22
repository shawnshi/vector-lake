---
id: "20260422_c7143i"
title: "Concept: Infini-attention"
type: "concept"
domain: "Artificial_Intelligence"
topic_cluster: "LLM_Architecture"
status: "Active"
alignment_score: 95
epistemic-status: "sprouting"
ttl: 1825
categories: ["Artificial_Intelligence"]
tags: ["Infinite_Context", "Compressive_Memory", "Transformer_Optimization"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/2404.07143v2.pdf"]
---

# Concept: Infini-attention

## 定义 (Definition)
**Infini-attention**（无限注意力）是一种结合了标准掩码局部注意力和带有线性循环相关矩阵的全局压缩记忆的注意力机制。它由 Google 提出，旨在打破 Transformer 模型处理超长序列时的内存瓶颈。

## 核心架构 (Core Architecture)
该机制将每一层注意力拆解为三个并行路径的融合：
1. **局部细节层 (Local Standard Attention)**: 处理当前上下文窗口内的精确信息。
2. **全局压缩层 (Global Compressive Memory)**: 将历史序列“压碎”成一个固定维度的线性矩阵。
3. **门控融合层 (Gated Fusion)**: 动态计算两者的权重平衡。

## 核心价值 (Value)
- **内存恒定性**: 实现 KV Cache 大小的固定化，不随序列长度增加而膨胀[^1]。
- **114x 压缩比**: 在 1M Token 的极端场景下，表现出惊人的存储效率。
- **增量更新**: 引入 Delta Rule 优化记忆质量，减少联想检索中的噪声。

## 医疗信息化应用场景 (HIT Scenarios)
- **长卷病历全景理解**: 对一名患者十年以上的历史影像报告、实验室结果和病程记录执行一次性非对称检索，而无需滑动窗口导致的语义截断。
- **[[Concept_Cerebellarization_1bit_LLM]]**: 为端侧小脑模型提供“处理无限过去”的能力。

[^1]: [[Source_2404.07143v2.md]]
