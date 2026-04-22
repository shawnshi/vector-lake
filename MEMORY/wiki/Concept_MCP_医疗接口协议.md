---
id: "20260422_c_mcp"
title: "Concept_MCP_医疗接口协议"
type: "concept"
domain: "Medical_IT"
topic_cluster: "Interoperability"
status: "Active"
alignment_score: 90
epistemic-status: "sprouting"
categories: ["System_Architecture"]
tags: ["MCP", "Model_Context_Protocol", "Interoperability", "Agentic_AI"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/DigitalHealthWeeklyBrief/DHWB-20260308.md", "raw/DigitalHealthWeeklyBrief/DHWB-20260406.md"]
---

# Concept_MCP_医疗接口协议 (Model Context Protocol)

## 定义
由 Gartner、CharmHealth 等推动的标准化协议，旨在为 AI 智能体（Agent）提供统一的 EHR 数据访问与上下文交互接口。

## 核心价值
- **打破数据垄断**: 削弱 HIS 厂商通过封闭接口锁定客户的能力[^1]。
- **智能体协同**: 允许跨厂商的 Agent 团队在统一的标准化网络下执行任务[^2]。
- **安全隔离**: 通过 MCP 网关可实现对第三方 Agent 的写操作审计[^1]。

## 战略挑战
- **语义主权**: 当接口民主化后，厂商的壁垒必须从“数据连接”迁移到 [[Concept_MSL_医疗语义层.md]] 的逻辑解释权。

[^1]: [[Source_DHWB-20260308.md]]
[^2]: [[Source_DHWB-20260406.md]]
