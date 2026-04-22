---
id: "20260422_sae01"
title: "稀疏自编码器 (Sparse Autoencoders)"
type: "concept"
domain: "Artificial_Intelligence"
topic_cluster: "Mechanistic_Interpretability"
status: "Active"
alignment_score: 97
epistemic-status: "evergreen"
ttl: 1825
categories: ["Artificial_Intelligence"]
tags: ["SAE", "可解释性", "特征解耦", "模型审计"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/2503.03601v1.md"]
---

# 稀疏自编码器 (Sparse Autoencoders)

## 核心定义
SAE 是一种无监督学习模型，旨在将大模型中纠缠在一起的“密集激活向量”映射到更低维但极度稀疏的特征空间，从而实现特征解耦。

## 核心机制
- **解决特征叠加 (Superposition)**: 针对神经网络中单个神经元同时编码多个无关概念的现象，SAE 通过强行拆解，将不可解释的黑盒切分为可独立观测的语义维度 [^1]。
- **单特征探测**: 能够提取出如“冗长说理”、“特定文体”、“特定事实”等具有明确物理含义的指示器。

## 战略价值
- **[[Concept_Algorithm_Audit.md]]**: 为 AI 监管提供了底层的物理审计工具，不再依赖于概率得分，而是基于特征指纹。
- **[[Concept_Mechanistic_Interpretability.md]]**: 是目前通向机械可解释性的核心路径。

[^1]: [[Source_2503.03601v1.md]]
