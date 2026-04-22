---
id: "20260422_c00002"
title: "Claim Denial Agents (拒付处理智能体)"
type: "concept"
domain: "Medical_IT"
topic_cluster: "Strategic_and_Business"
status: "Active"
alignment_score: 95
epistemic-status: "evergreen"
ttl: 730
categories: ["Healthcare_IT", "Strategy_and_Business"]
tags: ["RCM", "Agentic_AI", "ROI"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/HealthcareIndustryRadar/DHWB-Radar-20260406.md"]
---

# Claim Denial Agents (拒付处理智能体)

Claim Denial Agents 是一种专门用于自动处理医保拒付 (Claim Denials) 的 AI 智能体。它在医疗 IT 的 [[Concept_Agentic_Economy.md]] 落地中，被视为 ROI 最为清晰的“第一爆发点”。

## 核心功能

*   **自动申诉**: 基于医保局的审核规则，自动从电子病历中提取支持证据，并生成逻辑严密的申诉文档。
*   **合规审计**: 在费用上传前执行前置审计，识别潜在的合规风险点（如高值耗材使用不规范），防止拒付发生[^1]。
*   **模式识别**: 分析历史拒付数据，识别出特定的“拒付陷阱”，为医院管理层提供策略调整建议。

## 为什么是第一爆发点？

1.  **直接挽回经济损失**: 与临床 AI 改善预后的“软价值”不同，拒付处理 AI 直接关系到医院的现金流，价值极易量化。
2.  **规则驱动**: 拒付申诉本质上是逻辑与规则的博弈，非常适合大模型处理。
3.  **刚性需求**: 在 DRG/DIP 控费背景下，医院对防拒付的需求从“锦上添花”变成了“生存必需”。

## 相关实践
*   **MEDITECH**: 与 Google Cloud 合作推出了专门的拒付处理智能体。
*   **Epic**: 助手家族中的 **Penny** 承担了类似职责。

[^1]: [[Source_DHWB-Radar-20260406.md]]
