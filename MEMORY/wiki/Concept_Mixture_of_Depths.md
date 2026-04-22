---
id: "20260422_c_mod1"
title: "Mixture-of-Depths (MoD)"
type: "concept"
domain: "Artificial_Intelligence"
topic_cluster: "Model_Architecture"
status: "Active"
alignment_score: 90
epistemic-status: "sprouting"
ttl: 1825
categories: ["Artificial_Intelligence"]
tags: ["Dynamic_Compute", "Efficiency", "MoD"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/2404.02258v1.md"]
---

# Mixture-of-Depths (MoD)

## 定义
Mixture-of-Depths (MoD) 是一种 Transformer 架构的变体，它打破了“每个 Token 必须经过每一层计算”的传统限制。通过动态路由机制，模型在每一层只为最重要的部分 Token 分配完整的计算资源，其余 Token 则通过残差连接跳过，从而在宏观上实现“计算深度的混合” [属于:: [[Concept_Artificial_Intelligence.md]]]。

## 核心机制
1. **专家选择路由 (Expert-choice Routing)**: 与 MoE 由 Token 选择专家不同，MoD 由每一层（或 Block）设定一个固定的计算容量 $k$，并从序列中主动挑选权重最高的 $k$ 个 Token 进行计算。
2. **静态容量 (Static Capacity)**: 这种硬性配额确保了硬件计算效率的稳定性，避免了动态路由带来的负载不均衡。

## 战略价值
- **算力民主化**: 为 [[Concept_Cerebellarization_1bit_LLM.md]] 提供了理论支持。通过将不重要的 Token 降级处理，使高性能模型能够运行在受限的医疗边缘设备上。
- **信息密度过滤**: 模拟了人类注意力的分层机制，在处理海量临床数据时，自动过滤背景噪音（跳过），聚焦于高价值病征（完整计算）。

## 矛盾点
- **配额瓶颈**: 在处理信息极度密集的医疗紧急序列（如抢救室多维监测数据）时，固定的容量 $k$ 可能会导致部分关键细节被错误跳过 [反驳:: [[Concept_Zero_Tolerance_Threshold.md]]]。

[^1]: [[Source_2404.02258v1.md]]
