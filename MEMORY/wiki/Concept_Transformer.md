---
id: "20260422_c001trn"
title: "Concept: Transformer"
type: "concept"
domain: "Artificial_Intelligence"
topic_cluster: "Model_Architecture"
status: "Active"
alignment_score: 100
epistemic-status: "evergreen"
ttl: 1825
categories: ["Artificial_Intelligence"]
tags: ["Architecture", "Self-Attention", "LLM_Base"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/1706.03762v7.md"]
---

# Concept: Transformer

## 定义 (Definition)
**Transformer** 是一种基于自注意力机制的深度学习架构。它通过抛弃递归（Recurrence）结构，实现了计算的高度并行化，成为大语言模型（LLM）的物理地基 [支持:: [[Concept_Agentic_AI]]]。

## 核心机制 (Mechanisms)
1. **自注意力 (Self-Attention)**: 计算输入序列中每个单元与其他所有单元的相关权重。
2. **位置编码 (Positional Encoding)**: 在无序的矩阵运算中注入拓扑秩序。
3. **残差连接与层归一化**: 保证了深层网络的稳定训练。

## 医疗领域的张力 (Medical Context)
- **参数爆炸 vs 边缘降维**: Transformer 的 $O(n^2)$ 复杂度在处理长篇病历时面临算力挑战，推动了 **[[Concept_1-bit_LLM]]** 等降维技术的发展 [衍生自:: [[Concept_1-bit_LLM]]]。
- **序列偏置**: 相比 RNN，Transformer 缺乏显式的时序感，这在处理具备强因果逻辑的临床路径时可能产生幻觉。

## 演化关联 (Evolution)
- [对比:: [[Concept_Word2Vec]]]: Transformer 引入了动态上下文语义，而 Word2Vec 主要是静态向量。
- [衍生自:: [[Source_1706.03762v7.md]]]
