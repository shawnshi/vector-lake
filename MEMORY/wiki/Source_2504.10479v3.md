---
id: "20260422_s10479"
title: "Source: InternVL3: Scaling Open Multimodal Foundation Models"
type: "source"
domain: "Artificial_Intelligence"
topic_cluster: "Multimodal_Models"
status: "Active"
alignment_score: 90
epistemic-status: "seed"
ttl: 365
categories: ["Artificial_Intelligence"]
tags: ["InternVL", "Multimodal", "V2PE", "Computer_Vision", "OpenGVLab"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/2504.10479v3.md", "raw/Huggingface-Daily-Papers/2504.10479v3.pdf"]
---

# InternVL3: Scaling Open Multimodal Foundation Models

## 核心创新：原生多模态联合预训练
OpenGVLab 发布的 **InternVL3** 摒弃了传统的“文本模型 + 视觉投影层”的缝合架构，采用了 **原生多模态联合预训练 (Native Multimodal Pre-Training)**。这种范式在预训练阶段就将视觉与文本 token 放在同一语义空间进行优化，极大地提升了模型的图文理解深度[^1]。

## 关键技术：变量视觉位置编码 (V2PE)
为了处理超长视觉序列（长视频或超高分辨率图像），InternVL3 引入了 [Relation:: [[Concept_V2PE.md]]]。该技术通过在位置编码中注入“分数级”增量，解决了长序列下位置信息模糊的问题，在不显著增加计算开销的前提下实现了对海量视觉信息的精准感知。

## 性能表现
InternVL3 在 MMMU、DocVQA 等多个多模态基准测试中逼近 Gemini 2.5 Pro 等闭源 SOTA 模型，代表了开源多模态能力的顶尖水平。

## 医疗场景映射
- **医学影像早期融合**: 为 [Relation:: [[Concept_MSL_医疗语义层.md]]] 提供了技术路线参考——影像数据不应仅作为文本的附件，而应作为原生模态参与临床语义的构建。
- **长手术视频分析**: V2PE 技术可支撑对长达数小时的手术视频进行关键帧提取与异常检测。

[^1]: [[Source_2504.10479v3.md]]
