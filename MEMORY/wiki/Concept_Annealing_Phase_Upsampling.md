---
id: "20260422_aps01"
title: "退火期数据突击 (Annealing Phase Upsampling)"
type: "concept"
domain: "Artificial_Intelligence"
topic_cluster: "Training_Strategy"
status: "Active"
alignment_score: 93
epistemic-status: "seed"
ttl: 1825
categories: ["Artificial_Intelligence"]
tags: ["训练策略", "SmolLM2", "认知激活", "数据质量"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/2502.01061v3.md"]
---

# 退火期数据突击 (Annealing Phase Upsampling)

## 核心定义
退火期数据突击是指在模型预训练的最后阶段（即学习率衰减的退火期），集中喂入极高质量、高逻辑密度的垂直领域数据（如数学、代码、高质量医疗病历）。

## 技术价值
- **认知激活**: 这一策略能显著激活小参数模型的推理潜力，使其在训练末期实现逻辑能力的跨越式提升 [^1]。
- **效率优先**: 相比于全量数据的盲目堆砌，这种针对性的“最后冲刺”更具算力性价比。

## 医疗应用
在微调医疗专用模型时，可在退火阶段引入 **[[Concept_MSL_医疗语义层.md]]** 映射后的高质量语料，以强化模型对复杂医疗逻辑的掌握。

[^1]: [[Source_2502.01061v3.md]]
