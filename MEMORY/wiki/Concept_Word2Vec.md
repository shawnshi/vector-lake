---
id: "20260422_c002w2v"
title: "Concept: Word2Vec"
type: "concept"
domain: "Artificial_Intelligence"
topic_cluster: "NLP_Foundations"
status: "Active"
alignment_score: 85
epistemic-status: "evergreen"
ttl: 1825
categories: ["Artificial_Intelligence"]
tags: ["Embeddings", "NLP", "Simplicity"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/1301.3781v3.md"]
---

# Concept: Word2Vec

## 定义 (Definition)
**Word2Vec** 是由 Google 提出的词嵌入生成技术。它通过简单的双层神经网络，将单词映射到高维连续向量空间，捕捉单词间的语义相似度。

## 哲学启示 (Philosophy)
- **分布式假设**: “告诉我你的邻居是谁，我就知道你是谁。”
- **计算降维**: 证明了移除复杂非线性层后，依靠海量数据的“基态测量”也能获得极佳的语义表征 [支持:: [[Concept_Cerebellarization_1bit_LLM]]]。

## 局限性
- **静态表示**: 无法处理一词多义（如“苹果”在不同语境下的含义），这一缺陷由后来的 **[[Concept_Transformer]]** 修复 [对比:: [[Concept_Transformer]]]。

## 溯源
- [衍生自:: [[Source_1301.3781v3.md]]]
