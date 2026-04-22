---
id: "20260422_ttc01"
title: "推理侧算力缩放 (Test-time Compute)"
type: "concept"
domain: "Artificial_Intelligence"
topic_cluster: "Reasoning_Scaling"
status: "Active"
alignment_score: 96
epistemic-status: "seed"
ttl: 1825
categories: ["Artificial_Intelligence"]
tags: ["Scaling_Laws", "DeepSeek-R1", "Reasoning", "Cost_Tradeoff"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/2501.12948v2.md"]
---

# 推理侧算力缩放 (Test-time Compute)

## 核心定义
推理侧算力缩放（又称推理期计算）是指通过增加模型在生成答案时的计算量（如长路径搜索、自我博弈、反复校验）来换取更高质量的输出。

## 技术实现
- **长链推理 (Chain-of-Thought)**: 模型在输出最终答案前，显式地生成大量的 `<think>` 思考过程 [^1]。
- **自我纠错**: 模型在发现逻辑矛盾时能够主动回溯并重写推理路径 [^1]。

## 医疗摩擦点
- **实时性要求**: 医疗一线（如急诊、手术室）对响应速度有极高要求，Test-time Compute 带来的延迟可能触碰临床安全红线。
- **边缘部署成本**: 增加推理期算力意味着对端侧设备的性能要求上升，与医疗边缘化趋势存在物理层面的成本冲突。

## 关联页面
- [对比:: [[Concept_Probability_Machine.md]]]
- [支持:: [[Concept_Agentic_AI.md]]]

[^1]: [[Source_2501.12948v2.md]]
