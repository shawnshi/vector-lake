---
id: "20260422_s002w2v"
title: "Source: Efficient Estimation of Word Representations in Vector Space (Word2Vec)"
type: "source"
domain: "Artificial_Intelligence"
topic_cluster: "NLP_Foundations"
status: "Active"
alignment_score: 90
epistemic-status: "evergreen"
ttl: 3650
categories: ["Artificial_Intelligence"]
tags: ["Word2Vec", "Embeddings", "Distributional_Hypothesis", "Efficiency"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/1301.3781v3.md", "raw/Huggingface-Daily-Papers/1301.3781v3.pdf"]
---

# Source: Efficient Estimation of Word Representations in Vector Space (Word2Vec)

## 核心内容摘要 (Abstract)
本文由 Google 团队（Mikolov 等）于 2013 年发布，提出了 **[[Concept_Word2Vec]]** 架构。其核心贡献在于通过移除神经网络中的隐藏层，大幅降低了计算复杂度，使得在海量语料上训练高质量词向量（Embeddings）成为可能。

## 关键技术点 (Key Claims)
- **分布式假设 (Distributional Hypothesis)**: 词的语义由其上下文决定。
- **架构极简主义**: 提出了 CBOW（连续词袋模型）和 Skip-gram 两种架构，舍弃了传统神经网络的非线性隐藏层以换取计算效率 [^1]。
- **线性语义特性**: 词向量在空间中展现出加法合成性（如 "King - Man + Woman = Queen"） [^1]。

## 历史地位 (Significance)
- 开启了深度学习在 NLP 领域的平民化进程。
- 证明了“简单架构 + 极大海量数据”在语义表征上的优越性。

[^1]: [[Source_1301.3781v3.md]]
