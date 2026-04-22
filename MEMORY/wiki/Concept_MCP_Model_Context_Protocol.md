---
id: "20260422_mcp03"
title: "模型上下文协议 (MCP - Model Context Protocol)"
type: "concept"
domain: "Medical_IT"
topic_cluster: "General"
status: "Active"
alignment_score: 95
epistemic-status: "seed"
ttl: 365
categories: ["System_Architecture"]
tags: ["互操作性", "EHR", "Agentic_AI"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/DigitalHealthWeeklyBrief/DHWB-20260315.md"]
---

# 模型上下文协议 (MCP - Model Context Protocol)

## 定义
MCP 是一种标准化协议，旨在允许 **[[Concept_Agentic_AI.md]]** 以安全、结构化的方式访问和交互 EHR (电子病历) 中的临床上下文。

## 战略影响
1. **去中心化**: MCP 可能终结 HIS 厂商对数据接口的“收税权”，让第三方 Agent 能无缝接入[^1]。
2. **集成效率**: 极大降低了 AI 应用与不同 EHR 系统对接的工程摩擦。
3. **护城河位移**: 迫使 HIS 厂商从“存储数据”转向“在 MSL 层建立逻辑规约”来维持壁垒。

## 关联页面
- [对比:: [[Concept_HL7_FHIR.md]]]
- [支持:: [[Concept_Agentic_AI.md]]]
- [衍生于:: [[Concept_MSL_医疗语义层.md]]]

[^1]: [[Source_DHWB-20260315.md]]
