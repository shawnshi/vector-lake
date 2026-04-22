---
id: "20260422_c002"
title: "Concept: T2A (Text-to-Action)"
type: "concept"
domain: "Medical_IT"
topic_cluster: "Artificial_Intelligence"
status: "Active"
alignment_score: 100
epistemic-status: "seed"
ttl: 1825
categories: ["Artificial_Intelligence"]
tags: ["Agentic AI", "自动化", "临床闭环", "卫宁健康"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/digitalhealthobserve/卫宁健康：在 Agentic AI 时代的竞争壁垒重构_2026-02-12.md"]
---

# Concept: T2A (Text-to-Action)

## 定义
T2A 指医疗系统从理解**自然语言意图 (Text)** 直接跃迁到**执行系统动作 (Action)** 的过程。它是 Agentic AI 在医疗领域落地的核心能力，标志着系统从“记录器”向“执行器”的进化。

## 物理实现路径
1. **意图捕获**: 医生通过语音或文字输入临床意图。
2. **语义对齐**: 通过 [[Concept_MSL_医疗语义层]] 将自然语言翻译为结构化的医疗逻辑。
3. **协作调度**: 由 [[Concept_ACE_智能体协作引擎]] 分配任务给不同的功能 Agent。
4. **动作闭环**: 调用 API 执行开立医嘱、修改病历、预约检查等真实系统操作。

## 核心挑战
- **安全红线**: 必须具备 99.9% 的确定性，防止 AI 产生误动作。通常需要 [支持:: [[Concept_Human-on-the-Loop]]] 的强制核对机制。
- **孤岛突破**: 需要底层系统（如 HIS/EMR）具备深度的 API 开放性。

## 战略价值
- 彻底消除 [支持:: [[Concept_表单监狱]]], 让医生回归临床。
- 实现真正的“临床闭环”，将意图转化为可审计的系统行为。

## 关联
- [属于:: [[Concept_Agentic_Hospital]]]
- [衍生于:: [[Concept_MSL_医疗语义层]]]
- [支持:: [[Concept_ACE_智能体协作引擎]]]
