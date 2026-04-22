---
id: "20260422_mod001"
title: "Source: Mixture-of-Depths: Dynamically allocating compute in transformer-based language models"
type: "source"
domain: "Artificial_Intelligence"
topic_cluster: "Model_Architecture"
status: "Active"
alignment_score: 92
epistemic-status: "sprouting"
ttl: 730
categories: ["Artificial_Intelligence"]
tags: ["Compute_Efficiency", "Dynamic_Depth", "Transformer", "MoD"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/2404.02258v1.md", "raw/Huggingface-Daily-Papers/2404.02258v1.pdf"]
---

# Mixture-of-Depths: Dynamically allocating compute in transformer-based language models

## 核心贡献
本文提出了一种名为 **Mixture-of-Depths (MoD)** 的架构，通过在 Transformer 层级动态分配计算资源，实现了在保持性能的前提下大幅提升推理效率。

## 关键技术机制
1. **Expert-choice Routing (专家选择路由)**: 不同于传统的 MoE（由 Token 选择专家），MoD 由网络层主动选择前 $k$ 个最重要的 Token 进行完整计算，其余 Token 通过残差连接（Residual Connection）跳过该层[^1]。
2. **Static Capacity Constraint (静态容量约束)**: 将计算量限制在固定的计算预算内，确保了硬件利用率的可预测性和效率[^1]。

## 主要发现
- **非等量计算需求**: 证明了并非序列中的所有 Token 都需要经过所有层的计算。
- **效率提升**: 在等效性能下，MoD 可以减少 50% 的单步 FLOPs，并实现更快的推理速度，同时训练效率也优于标准的 Transformer[^1]。

## 对 Vector Lake 的意义
- **[[Concept_Cerebellarization_1bit_LLM.md]] 的核心组件**: MoD 提供了“按需算力分配”的物理实现，是医疗边缘设备在有限带宽下实现复杂临床推理的关键。
- **[[Concept_Agentic_Hospital.md]] 的能效底座**: 在处理海量、冗余的医疗监测数据时，MoD 能够自动识别并“跳过”非关键信息，将算力集中在突发的异常指标上。

[^1]: [[Source_2404.02258v1.md]]
