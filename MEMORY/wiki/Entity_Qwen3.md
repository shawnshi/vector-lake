---
id: "20260422_e_qwen3"
title: "Entity: Qwen3 (阿里巴巴推理模型家族)"
type: "entity"
domain: "Artificial_Intelligence"
topic_cluster: "Reasoning_Models"
status: "Active"
alignment_score: 100
epistemic-status: "seed"
ttl: 1095
categories: ["Artificial_Intelligence"]
tags: ["System_2", "Logit_Distillation", "Thinking_Mode", "Alibaba"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/2505.09388v1.md"]
---

# Entity: Qwen3 (阿里巴巴推理模型家族)

## 核心定义
Qwen3 是阿里巴巴发布的下一代大语言模型家族，其核心特色是引入了原生的“思维模型 (Thinking Models)”架构。

## 关键特征
- **思维模式融合**: 统一了 System 1（快速直觉）与 System 2（深度逻辑）的训练与部署，支持通过 `/think` 标签显式调起推理过程。
- **高性能小模型**: 通过 **[[Concept_Reasoning_Distillation.md]]**，Qwen3 系列中的 3B、8B 模型展现出了极强的推理能力，甚至在多个基准测试中逼近 OpenAI-o1 [^1]。
- **Anytime 推理能力**: 支持 **[[Concept_Thinking_Budget.md]]** 调节，可根据任务复杂度动态分配算力成本。

## 战略定位
Qwen3 为医疗行业的“智力下沉”提供了重要底座。其小参数推理版模型是实现 **[[Concept_Cerebellarization_1bit_LLM.md]]** 的理想选择，有助于在资源受限的医疗边缘设备上部署高可靠的质控逻辑。

[^1]: [[Source_2505.09388v1.md]]
