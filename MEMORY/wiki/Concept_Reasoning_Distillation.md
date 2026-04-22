---
id: "20260422_rd001"
title: "推理能力蒸馏 (Reasoning Distillation)"
type: "concept"
domain: "Artificial_Intelligence"
topic_cluster: "Model_Compression"
status: "Active"
alignment_score: 97
epistemic-status: "seed"
ttl: 1825
categories: ["Artificial_Intelligence"]
tags: ["DeepSeek-R1", "Teacher-Student", "Knowledge_Transfer", "Efficiency"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/2501.12948v2.md"]
---

# 推理能力蒸馏 (Reasoning Distillation)

## 核心定义
推理能力蒸馏是一种将高性能大规模模型（Teacher Model）生成的长逻辑推理路径（Chain-of-Thought）作为训练数据，喂给参数量较小的模型（Student Model）的技术。

## 关键突破
- **DeepSeek-R1 实践**: 证实了通过 R1 生成的 80 万条高质量推理数据，可以使 7B、14B 甚至 1.5B 规模的模型获得远超其物理参数上限的逻辑能力 [^1]。
- **Qwen3 的 Logit 蒸馏 (Logit Distillation)**: 进一步将蒸馏对象从文本（Tokens）下沉到概率分布（Logits）。通过捕捉 Teacher 模型推理时的分布细节，Qwen3 成功将 o1 级别的推理能力高效迁移到了 3B/8B 级别的端侧模型中 [^2]。
- **非对称进化**: 这种范式打破了“参数量决定智力”的教条，使得“小参数、高逻辑”模型成为可能。

## 医疗应用前景
- **[[Concept_Cerebellarization_1bit_LLM.md]]**: 是实现医疗模型“小脑化”的关键路径，支持在低功耗硬件上实现高确定性的临床逻辑校验。
- **[[Concept_Skill_Subscription_Model.md]]**: 医院可订阅基于顶级模型蒸馏出的垂直领域“技能包”，实现能力的快速私有化部署。

[^1]: [[Source_2501.12948v2.md]]
[^2]: [[Source_2505.09388v1.md]]
