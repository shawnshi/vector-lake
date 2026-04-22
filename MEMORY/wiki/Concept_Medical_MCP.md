---
id: "20260422_mcp01"
title: "Concept: Medical MCP (Model Context Protocol)"
type: "concept"
domain: "System_Architecture"
topic_cluster: "General"
status: "Active"
alignment_score: 96
epistemic-status: "evergreen"
ttl: 1825
categories: ["System_Architecture", "Healthcare_IT"]
tags: ["MCP", "Protocol", "Agent_Access", "athenahealth", "语义定义权"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/DigitalHealthWeeklyBrief/DHWB-20260413.md", "raw/HealthcareIndustryRadar/DHWB-Radar-20260315.md"]
---

# Concept: Medical MCP (Model Context Protocol)

## 定义
Medical MCP 是 athenahealth 等厂商在 HIMSS26 倡导的医疗模型上下文协议标准。它通过定义统一的“适配器”规范，使外部 AI 智能体能够以标准化、安全的方式访问 EHR 中的实时临床上下文[^1][^2]。

## 核心作用
1.  **打通集成深渊**: 解决了 AI 代理接入不同 EHR 系统时的非标对接难题，降低了集成成本。
2.  **语义定义权之争**: 谁掌控了 MCP 标准，谁就掌控了医疗 AI 的“数据入口”与“话语权”。这是全球 HIT 厂商在 **[[Concept_Logic_Sovereignty.md]]** 上的新博弈点[^2]。
3.  **受控访问**: 为 Agent 确立了受控、可审计的访问路径，防止模型产生幻觉式写入。

## 厂商立场
- **athenahealth**: 标准发起者，试图通过开放协议打破 Epic/Cerner 的生态垄断[^2]。
- **卫宁健康**: 发布兼容层，在支持标准的同时，强制要求写操作通过自身的 [[Concept_MSL_医疗语义层.md]] 进行加固审计[^2]。

[^1]: [[Source_DHWB-20260413.md]]
[^2]: [[Source_DHWB-Radar-20260315.md]]
