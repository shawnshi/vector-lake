---
id: "20260422_smollm01"
title: "SmolLM2"
type: "entity"
domain: "Artificial_Intelligence"
topic_cluster: "Small_Models"
status: "Active"
alignment_score: 94
epistemic-status: "seed"
ttl: 1095
categories: ["Artificial_Intelligence"]
tags: ["轻量化模型", "数据驱动", "1.7B", "边缘推理"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/2502.01061v3.md"]
---

# SmolLM2

## 概览
SmolLM2 是由 Hugging Face 团队开发的一系列轻量化大语言模型，主要参数规模为 1.7B。它代表了通过“小参数、高质量数据”实现极致性价比的演进路线。

## 技术亮点
- **动态训练策略**: 打破了静态数据配比的教条，引入了 **[[Concept_Annealing_Phase_Upsampling.md]]**（退火期数据突击）技术 [^1]。
- **自动化打分**: 利用 **[[Entity_Llama-3-70B.md]]** 作为评判员，实现训练数据的工业化清洗与优选。

## 医疗应用价值
- **[[Concept_Cerebellarization_1bit_LLM.md]]**: SmolLM2 及其蒸馏版本是实现医疗边缘设备（如手持超声、病房监控仪）“小脑化”推理的理想载体。
- **低功耗计算**: 在维持基础临床语义理解能力的同时，极大降低了对医院基础设施的算力负荷。

[^1]: [[Source_2502.01061v3.md]]
