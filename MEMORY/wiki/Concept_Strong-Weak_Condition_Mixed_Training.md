---
id: "20260422_swc01"
title: "强弱条件混合训练 (Strong-Weak Condition Mixed Training)"
type: "concept"
domain: "Artificial_Intelligence"
topic_cluster: "Multimodal_Training"
status: "Active"
alignment_score: 91
epistemic-status: "seed"
ttl: 1825
categories: ["Artificial_Intelligence"]
tags: ["OmniHuman", "多模态", "数据效率", "鲁棒性"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/2502.02737v1.md"]
---

# 强弱条件混合训练 (Strong-Weak Condition Mixed Training)

## 核心定义
强弱条件混合训练是一种多模态学习策略，通过引入易于获取且约束力强的信号（强条件，如姿态、文本）来引导模型学习难以捕捉或容易产生歧义的信号（弱条件，如音频细节）。

## 技术突破
- **数据废墟回收**: OmniHuman 证明了该策略可以利用原本被弃用的、背景复杂的低质量视频数据，极大地提升了训练数据的覆盖度 [^1]。

## 医疗意义
- **低资源多模态**: 在医疗影像或语音数据存在大量噪声、标注困难的情况下，可利用结构化的 EMR（强条件）带动影像序列（弱条件）的语义关联学习。

[^1]: [[Source_2502.02737v1.md]]
