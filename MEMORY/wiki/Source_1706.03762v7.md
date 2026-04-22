---
id: "20260422_s003trn"
title: "Source: Attention Is All You Need (Transformer)"
type: "source"
domain: "Artificial_Intelligence"
topic_cluster: "Model_Architecture"
status: "Active"
alignment_score: 95
epistemic-status: "evergreen"
ttl: 3650
categories: ["Artificial_Intelligence"]
tags: ["Transformer", "Self-Attention", "Parallelization", "Foundational_Model"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/1706.03762v7.md", "raw/Huggingface-Daily-Papers/1706.03762v7.pdf"]
---

# Source: Attention Is All You Need (Transformer)

## 核心内容摘要 (Abstract)
Google 团队于 2017 年提出的奠基性论文，彻底摒弃了循环神经网络（RNN）和卷积神经网络（CNN），提出了完全基于 **[[Concept_Self-Attention]]**（自注意力机制）的 **[[Concept_Transformer]]** 架构。

## 关键创新 (Key Innovations)
- **自注意力机制 (Self-Attention)**: 允许模型并行处理序列，并直接计算序列中任意两个位置的依赖关系，解决了长程依赖问题 [^1]。
- **位置编码 (Positional Encoding)**: 由于移除了循环结构，通过在输入中注入位置正弦/余弦信号来硬编码序列的时序信息 [^1]。
- **多头注意力 (Multi-Head Attention)**: 允许模型在不同的表示子空间中并行学习信息 [^1]。

## 行业影响 (Impact)
- 催生了包括 GPT、BERT 在内的所有现代大语言模型（LLM）。
- 将 NLP 的范式从“序列处理”转变为“空间矩阵运算”。

[^1]: [[Source_1706.03762v7.md]]
