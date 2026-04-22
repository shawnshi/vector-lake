---
id: "20260422_c008"
title: "Concept: 工作流粘性 (Workflow Stickiness)"
type: "concept"
domain: "Healthcare_IT"
topic_cluster: "Strategy"
status: "Active"
alignment_score: 95
epistemic-status: "evergreen"
ttl: 1825
categories: ["Healthcare_IT", "Strategy_and_Business"]
tags: ["护城河", "嵌入式集成", "迁移成本"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗大语言模型应用二十讲/模块三：方案构建 —— 从“功能设计”到“系统融合”与“风险控制”.md"]
---

# Concept: 工作流粘性 (Workflow Stickiness)

## 定义
工作流粘性是指 AI 应用通过深度嵌入核心临床/运营流程，构建起的高昂物理迁移成本。它是 AI 原生时代厂商真正的“硬护城河”。

## 实施路径
- **嵌入式集成**: AI 不是一个独立的 Tab 或 App，而是静默运行在 EMR/HIS 编辑器中的智能引擎 [支持:: [[Concept_Embedded_Experience.md]]]。
- **数据回写**: 将 AI 洞察实时、自动化地回写至业务数据库，形成闭环。
- **动态进化 (进化飞轮)**: 通过持续捕获 **[[Concept_Flow_Process_Data.md]]** 实现模型性能的非线性提升。一旦用户修正行为被转化为资产，切换成本将从“软件习惯”跃迁为“智力进化赤字”[^1]。
- **习惯养成**: 改变用户的操作肌肉记忆，使离开 AI 的工作流变得不可忍受。

## 关联概念
- [支持:: [[Concept_架构包围算法]]]
- [支持:: [[System_WiNEX]]]
- [支持:: [[Concept_Evolution_Flywheel]]]

[^1]: [[Source_第十五讲：构建真护城河 ——驱动“过程数据”的进化飞轮.md]]
