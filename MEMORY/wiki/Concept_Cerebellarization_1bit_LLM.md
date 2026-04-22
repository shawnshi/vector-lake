---
id: "20260422_c_cereb001"
title: "Concept: 小脑化 (Cerebellarization)"
type: "concept"
domain: "System_Architecture"
topic_cluster: "Edge_AI"
status: "Active"
alignment_score: 100
epistemic-status: "sprouting"
ttl: 1825
categories: ["System_Architecture", "Artificial_Intelligence", "Healthcare_IT"]
tags: ["Edge_Computing", "Low_Latency", "Quality_Control", "1bit_LLM", "Qwen3"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/Huggingface-Daily-Papers/2502.01061v3.md", "raw/Huggingface-Daily-Papers/2505.09388v1.md"]
---

# Concept: 小脑化 (Cerebellarization)

## 核心定义
小脑化是指在分布式智能系统中，将高频、实时、高确定性的指令执行与质控任务下放至具备边缘算力的“小脑模型”处理，而将复杂的非结构化推理留给云端“大脑模型”的架构范式。

## 物理实现路径
1. **1-bit 架构**: 利用 **[[Concept_1-bit_LLM.md]]** 技术极大地压缩模型参数，使其能在极低功耗的医疗传感器、监控仪中运行。
2. **推理能力下沉**: 借助 **[[Concept_Reasoning_Distillation.md]]**，如 Qwen3 的 Logit 蒸馏技术，使得 3B 甚至更小的模型也能具备可靠的临床逻辑校验能力 [^2]。
3. **实时围栏**: 建立物理级的 **[[Concept_Thinking_Budget.md]]** 约束，确保“小脑”在毫秒级内给出反馈，防止医疗决策流程中的逻辑阻塞。

## 医疗应用场景
- **端侧临床路径监控**: 在输液泵、监护仪中嵌入小脑模型，实时拦截违规医嘱。
- **高频文书自动补全**: 在医生输入时，由小脑模型负责低认知负荷的文本预测与规范性检查，减少大脑（医生与云端模型）的介入频率。

[^1]: [[Source_2502.01061v3.md]]
[^2]: [[Source_2505.09388v1.md]]
