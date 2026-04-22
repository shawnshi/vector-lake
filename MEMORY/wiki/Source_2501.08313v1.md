---
id: "20260422_s08313"
title: "Source: MiniMax-01: Scaling Foundation Models with Lightning Attention"
type: "source"
domain: "Artificial_Intelligence"
topic_cluster: "Hybrid_Architecture"
status: "Active"
alignment_score: 92
epistemic-status: "seed"
ttl: 365
categories: ["Artificial_Intelligence"]
tags: ["MiniMax-01", "Lightning_Attention", "Hybrid_Architecture", "Long_Context", "MiniMax"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/2501.08313v1.md", "raw/Huggingface-Daily-Papers/2501.08313v1.pdf"]
---

# MiniMax-01: Scaling Foundation Models with Lightning Attention

## 概览 (Overview)
MiniMax (名之诺) 发布的 **MiniMax-01** 是一款采用混合架构（Hybrid Architecture）的领先基础模型。该模型成功证明了线性注意力机制（Linear Attention）与标准 Softmax 注意力的混合使用，在大规模长文本处理中兼具效率与精度。

## 核心架构 (Architecture)
- **Hybrid-lightning Attention**: 采用 7:1 的层级比例，即 7 层 Lightning Attention（线性复杂度 $O(N)$）配合 1 层标准 Softmax Attention（平方复杂度 $O(N^2)$）。
- **算力分配**: Lightning Attention 负责处理海量背景信息，Softmax 层负责精确的端到端检索（大海捞针能力）。
- **4M Token 支持**: 证明了在该架构下，预填充延迟随长度线性增长，远优于传统 Transformer。

## 关键技术 (Techniques)
- **LASP+ & Varlen Ring Attention**: 优化的长序列并行算法，通过掩盖通信开销提升训练效率。
- **多模态对齐**: 展示了该架构在处理视频长卷及超长代码库方面的优势。

## 对知识库的意义 (Significance)
- **[[Concept_Hybrid_Attention]]**: 提供了一种非对称的算力竞争方案——用线性效率承接医疗大数据，用 Softmax 锚点保住关键诊断精度。
- **[[Concept_Software_Liquefaction]]**: 混合架构使得软件能够动态适应超长上下文的“液态”需求。

[^1]: [[Source_2501.08313v1.md]]
