---
id: "20260422_croa"
title: "Concept_Resilience_Oriented_Architecture"
type: "concept"
domain: "Medical_IT"
topic_cluster: "System_Architecture"
status: "Active"
alignment_score: 100
epistemic-status: "evergreen"
ttl: 1825
categories: ["System_Architecture"]
tags: ["架构设计", "韧性架构", "安全阀", "反脆弱", "RAG"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗大语言模型应用二十讲/第十三讲：架构之道 —— “RAG优先”与内置“安全阀”.md"]
---

# 韧性导向架构 (Resilience-Oriented Architecture)

## 定义
**韧性导向架构**是专为医疗等高风险环境设计的 AI 系统架构范式。其核心思想是“为意外而设计”，通过多层级的刚性约束 and 动态熔断机制，确保系统在 AI 发生幻觉或外部环境剧变时仍能保持核心功能的稳定。

## 核心支柱
1.  **RAG 优先（RAG-First）**：
    *   将“逻辑推理”与“知识真实性”解耦。
    *   模型仅作为调度器 and 推理机，不直接通过权重存储医学事实。
    *   利用外部可审计的知识库作为唯一真理来源[支持:: [[Concept_Evidence_Mesh.md]]]。
2.  **内置安全阀（Safety Valves）**：
    *   **拦截层**：输出前的合规性与安全性过滤（脱敏、禁语）。
    *   **校验层**：强制性的事实核对（Fact-Checking）逻辑。
    *   **降级层**：支持自动切换至规则引擎或人工流程的[[Concept_Graceful_Degradation.md]]机制。
3.  **可观测性（Observability）**：
    *   记录 AI 的每一次推理路径（Thought Trace）。
    *   对模型信心度进行实时监控，低于阈值强制触发“摩擦力”。

## 战略价值
*   **责任清晰**：通过架构隔离，明确 AI 的决策边界。
*   **反脆弱性**：即使底层模型升级或失效，整体业务逻辑依然可控[^1]。

## 关联导航
*   核心技术：[支持:: [[Concept_RAG.md]]]
*   防御手段：[支持:: [[Concept_Evidence_Mesh.md]]]
*   对标Source：[属于:: [[Source_第十三讲：架构之道 —— “RAG优先”与内置“安全阀”.md]]]

[^1]: [[Source_第十三讲：架构之道 —— “RAG优先”与内置“安全阀”.md]]
