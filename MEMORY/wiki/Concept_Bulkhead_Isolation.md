---
id: "20260422_bulk01"
title: "Concept: 舱壁模式 (Bulkhead Isolation)"
type: "concept"
domain: "System_Architecture"
topic_cluster: "General"
status: "Active"
alignment_score: 95
epistemic-status: "seed"
ttl: 1825
categories: ["System_Architecture"]
tags: ["容错", "隔离", "Agent", "HIS", "并发控制"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/DigitalHealthWeeklyBrief/DHWB-20260413.md", "raw/DigitalHealthWeeklyBrief/DHWB-20260419.md"]
---

# Concept: 舱壁模式 (Bulkhead Isolation)

## 定义
舱壁模式借鉴自造船工程与金融高频交易系统。在医疗 IT 架构中，它指将 AI 智能体的写操作、大规模推理请求与 HIS 核心的医保结算、挂号、收费等关键交易流执行物理/逻辑层面的绝对隔离[^1]。

## 核心必要性
- **防系统击穿**: 防止高并发、不可预测的 Agent 请求（如全院级病历质控或多维分析）占满数据库连接池，从而导致 HIS 系统在早高峰等关键时段宕机[^1]。
- **代币化授权 (Tokenization)**: 结合代币化机制，对 Agent 的每一次写入执行原子级授权，确保操作的可逆性与安全性[^2]。

## 战略落地
- **卫宁 WiNEX**: 必须引入此类金融级容错架构，实现对竞品的“零宕机、零掉单”降维打击[^1]。

## 关联页面
- [支持:: [[Concept_Agentic_AI.md]]]
- [技术手段:: [[Concept_MSL_医疗语义层.md]]]

[^1]: [[Source_DHWB-20260413.md]]
[^2]: [[Source_DHWB-20260419.md]]
