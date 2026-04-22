---
id: "20260422_qwen01"
title: "Qwen2.5-VL"
type: "entity"
domain: "Artificial_Intelligence"
topic_cluster: "Multimodal_Models"
status: "Active"
alignment_score: 95
epistemic-status: "seed"
ttl: 1095
categories: ["Artificial_Intelligence"]
tags: ["多模态模型", "阿里巴巴", "开源模型", "视觉理解"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/2502.13923v1.md"]
---

# Qwen2.5-VL

## 概览
Qwen2.5-VL 是阿里巴巴 Qwen 团队开发的新一代开源多模态大语言模型。它在处理原生动态分辨率图像和长视频方面具有显著优势。

## 技术特性
- **时空一致性**: 通过 **[[Concept_MRoPE.md]]** 实现了绝对时间戳对齐，解决了变帧率视频的时间失真问题 [^1]。
- **极致效率**: 采用 **[[Concept_Window_Attention.md]]** 显著降低了处理高分辨率视觉输入的算力需求 [^1]。

## 医疗落地场景
- **手术视频结构化**: 基于其强大的时空感知能力，可用于自动化手术视频标注与质量审计。
- **医学文档解析**: 结合其 HTML 空间锚定技术，能够极高精度地还原复杂医疗报告中的表格与公式。

[^1]: [[Source_2502.13923v1.md]]
