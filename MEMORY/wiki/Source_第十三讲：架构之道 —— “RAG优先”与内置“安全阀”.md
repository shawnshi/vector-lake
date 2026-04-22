---
id: "20260422_s13rag"
title: "Source_第十三讲：架构之道 —— “RAG优先”与内置“安全阀”"
type: "source"
domain: "Medical_IT"
topic_cluster: "Architecture"
status: "Active"
alignment_score: 96
epistemic-status: "evergreen"
ttl: 365
categories: ["System_Architecture"]
tags: ["RAG", "安全阀", "系统韧性"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗大语言模型应用二十讲/第十三讲：架构之道 —— “RAG优先”与内置“安全阀”.md"]
---

# 第十三讲：架构之道 —— “RAG优先”与内置“安全阀”

## 核心内容摘要
本讲确立了医疗 AI 应用的架构铁律：RAG（检索增强生成）不是一种选项，而是对抗幻觉、保障医疗安全的底线。同时，提出在架构中必须内置物理层的“安全阀”（熔断机制）。

## 关键主张
1. **RAG 优先原则**: 知识（外部数据库）与推理（模型）解耦，确保每一句 AI 输出都有据可查 [支持:: [[Concept_Evidence_Mesh.md]]]。
2. **内置安全阀**: 
    - **脱敏网关**: 强制过滤隐私数据。
    - **内容过滤器**: 拦截高风险医学建议。
    - **物理熔断**: 异常状态下自动降级为传统工作流[^1]。

## 关联节点
- [底层支撑:: [[Concept_RAG.md]]]
- [设计模式:: [[Concept_Graceful_Degradation.md]]]

[^1]: [[Source_第十三讲：架构之道 —— “RAG优先”与内置“安全阀”.md]]
