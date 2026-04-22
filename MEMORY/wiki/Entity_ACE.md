---
id: "20260422_e006ace"
title: "ACE (Agent Coordination Engine)"
type: "entity"
domain: "Medical_IT"
topic_cluster: "AI_Architecture"
status: "Active"
alignment_score: 100
epistemic-status: "evergreen"
categories: ["System_Architecture", "Healthcare_IT"]
tags: ["ACE", "智能体协调引擎", "Skills", "任务调度"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/research/AI 原生医院 Skills 库建设与应用路径.md"]
---

# ACE (Agent Coordination Engine)

## 定义
ACE 是卫宁健康（[[Entity_Winning_Health.md]]）Agentic AI 架构中的核心指挥中枢。它负责协调多个原子化的 **Skills** 与专岗智能体，实现复杂临床意图的闭环执行[^1]。

## 核心职责
- **上下文工程 (Context Engineering)**：负责 Context 的构建、更新与评估，确保 AI 决策具备高保真的业务背景[^1]。
- **意图路由**：通过 [[Concept_MSL_医疗语义层.md]] 的翻译结果，将任务分发给最合适的 Skill 能力单元。
- **逻辑校验**：在动作执行前执行医学红线与合规约束的“非对称审计”[^1]。

## 地位
它是实现 [[Concept_T2A_Text-to-Action.md]] 的关键执行底座，是 AI 原生医院的“调度小脑”[^1]。

[^1]: [[Source_AI 原生医院 Skills 库建设与应用路径.md]]
