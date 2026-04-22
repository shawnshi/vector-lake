---
id: "20260422_deepseek_r1"
title: "DeepSeek R1"
type: "entity"
domain: "Artificial_Intelligence"
topic_cluster: "Reasoning_Models"
status: "Active"
alignment_score: 98
epistemic-status: "evergreen"
categories: ["Artificial_Intelligence"]
tags: ["开源大模型", "医疗推理", "强化学习", "GRPO", "推理蒸馏"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/DigitalHealthLecturesScout/Weekly_DigitalHealth_20260329.md", "raw/Huggingface-Daily-Papers/2501.12948v2.md"]
---

# DeepSeek R1

## 概览
DeepSeek R1 是由 DeepSeek 发布的开源推理大模型系列。它通过创新的强化学习（RL）路径，证实了模型可以在不依赖大规模人工标注（SFT）的情况下，通过自主进化涌现出复杂的逻辑推理与自我纠错能力。

## 核心技术特性
- **纯 RL 驱动 (R1-Zero)**: 验证了强化学习作为激发 LLM 推理能力的第一性原理 [^2]。
- **GRPO 算法**: 引入 **[[Concept_GRPO.md]]** 取消了独立奖励模型，极大地提升了训练效率并降低了硬件门槛 [^2]。
- **推理蒸馏**: 将 R1 的逻辑推演路径蒸馏至 1.5B/7B/14B/32B/70B 等不同参数规模的模型中，实现了性能的跨级跨越 [^2]。

## 医疗能力与应用
- **推理平权**: 使得低成本的本地化部署模型具备了与顶级闭源模型相当的医疗合规性审计能力。
- **心脏病学测试**: 在专业测试中展现出超越人类专家的性能，适合处理复杂的临床决策支持（CDSS）[^1]。
- **逻辑底座**: 适合作为 **[[Concept_Limbic_Reasoning_Layer.md]]** 的物理底座，纠正医疗流程中的概率性偏差。

## 行业评价
DeepSeek R1 的出现再次验证了 **[[Concept_The_Bitter_Lesson.md]]**：在大规模计算与客观规则面前，人类有限的经验标注终将成为瓶颈。

[^1]: [[Source_Weekly_DigitalHealth_20260329.md]]
[^2]: [[Source_2501.12948v2.md]]
