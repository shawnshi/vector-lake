---
id: "20260422_wa01"
title: "动态窗口注意力 (Window Attention)"
type: "concept"
domain: "Artificial_Intelligence"
topic_cluster: "Model_Architecture"
status: "Active"
alignment_score: 94
epistemic-status: "seed"
ttl: 1825
categories: ["Artificial_Intelligence"]
tags: ["注意力机制", "计算效率", "视觉编码", "Qwen2.5-VL"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/2502.13923v1.md"]
---

# 动态窗口注意力 (Window Attention)

## 核心定义
在视觉 Transformer (ViT) 架构中，将大部分全局注意力层替换为受限范围的局部窗口计算（通常为 112x112 窗口），仅保留极少数全局层进行语义缝合。

## 解决痛点
- **计算复杂度爆炸**: 将 ViT 对高分辨率图像处理的二次方复杂度强制压缩至线性，使得模型能够处理原生 2K 以上分辨率的图像而不崩溃 [^1]。
- **特征局部性**: 充分利用了视觉特征在空间上的局部相关性，减少了冗余的全局计算。

## 工业意义
- **[[Concept_Edge_Agent.md]]**: 使得多模态模型在端侧设备（如手机、医疗终端）上处理高分文档解析成为可能。

[^1]: [[Source_2502.13923v1.md]]
