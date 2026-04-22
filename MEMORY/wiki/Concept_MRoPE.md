---
id: "20260422_mrope01"
title: "绝对时间对齐位置编码 (Absolute Time MRoPE)"
type: "concept"
domain: "Artificial_Intelligence"
topic_cluster: "Model_Architecture"
status: "Active"
alignment_score: 96
epistemic-status: "seed"
ttl: 1825
categories: ["Artificial_Intelligence"]
tags: ["位置编码", "MRoPE", "视频理解", "时间语义"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/2502.13923v1.md"]
---

# 绝对时间对齐位置编码 (Absolute Time MRoPE)

## 核心定义
MRoPE (Multimodal Rotary Position Embedding) 的一种进化形式，将位置编码的时间维度直接与视频的绝对时间戳（Absolute Time）绑定，而非传统的帧序号。

## 技术优势
- **物理时间感知**: 模型能够通过位置 ID 的间隔直接感知物理时间的流逝节奏，解决了变帧率（Variable FPS）导致的语义失真 [^1]。
- **无损时空感知**: 在处理原生视频流时，无需通过额外的帧数计算头即可维持时间序列的连贯性。

## 关联概念
- **[[Entity_Qwen2.5-VL.md]]**: 首个大规模应用该技术实现 SOTA 视频理解性能的模型。

[^1]: [[Source_2502.13923v1.md]]
