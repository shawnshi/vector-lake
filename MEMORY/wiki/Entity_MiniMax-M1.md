---
id: "20260422_e_minimax"
title: "Entity: MiniMax-M1"
type: "entity"
domain: "Artificial_Intelligence"
topic_cluster: "Reasoning_Models"
status: "Active"
alignment_score: 95
epistemic-status: "seed"
ttl: 1095
categories: ["Artificial_Intelligence"]
tags: ["MiniMax", "Reasoning_Model", "Long-Context", "Hybrid_Attention"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/2505.24726v1.pdf"]
---

# Entity: MiniMax-M1

## 定义与背景
**MiniMax-M1** 是由 MiniMax 开发的一系列专注于长文本推理的大语言模型。它是国内首个公开宣称在 1M 上下文级别实现强化学习推理训练的模型系列。

## 技术特征
*   **架构创新**: 采用了 **[[Concept_Hybrid_Lightning_Attention.md]]**，通过线性注意力大幅降低长序列生成的显存与计算开销。
*   **训练算法**: 核心驱动力为 **[[Concept_CISPO.md]]**，这是一种改进版的 GRPO，专为解决长思维链中的信度分配与顿悟（Insight）捕捉而设计[^1]。
*   **算力基座**: 在 **[[Hardware_H800_GPU_Cluster.md]]** 上完成了高效迭代。

## 战略意义
MiniMax-M1 的出现标志着推理模型竞争已从“短文本逻辑测试（如 GSM8K）”转向“长文本、高复杂度、低生成成本”的综合工程博弈。它是 **[[Concept_Cerebellarization_1bit_LLM.md]]**（小脑化）在长文本推理领域的重要实践，旨在让模型在处理全量长病历、长代码库时具备廉价且深度的推理能力。

## 关联页面
*   **对比:: [[Entity_DeepSeek_R1.md]]**: DeepSeek-R1 侧重于纯 Softmax 的强化学习探索，而 MiniMax-M1 侧重于架构上的长文本经济性。
*   **支持:: [[Concept_Agentic_AI.md]]**: 为复杂、长程的智能体任务提供了算力可负担的底座。

[^1]: [[Source_2505.24726v1.md]]
