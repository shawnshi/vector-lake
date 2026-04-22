---
id: "20260422_e001cis"
title: "Entity_CIS (临床信息系统)"
type: "entity"
domain: "Medical_IT"
topic_cluster: "Clinical_System"
status: "Active"
alignment_score: 100
epistemic-status: "evergreen"
categories: ["Healthcare_IT"]
tags: ["CIS", "生产执行系统", "临床应用", "过程管控"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗数字化三十讲/第九讲：临床路径与闭环管理：医嘱、护理、手术麻醉、重症监护系统.md"]
---

# Entity: 临床信息系统 (CIS)

## 定义
临床信息系统（Clinical Information System）是以病人为中心，直接服务于临床医疗活动的系统集合。它被定义为医疗“生产车间”的 **MES (生产执行系统)**，其核心逻辑是从“结果登记”向“过程管控”演进[^1]。

## 子系统构成
- **[[Entity_CPOE.md]]**: 医嘱处理系统。
- **EMR (电子病历)**: 临床记录核心。
- **AIMS & ICU-IS**: 手术与重症专业系统。
- **移动护理**: 执行端闭环。

## 核心哲学
- **过程优于结果**: CIS 的价值在于对医疗行为的实时监督与引导[^1]。
- **[[Concept_Closed_Loop_Management.md]]**: 确保每一条医嘱都有始有终。
- **路径约束**: 将 **[[Concept_Clinical_Pathway.md]]** 固化在工作流中。

## 关联节点
- [支持:: [[Concept_Clinical_Pathway.md]]]
- [属于:: [[Concept_Medical_Fortress_Four_Walls.md]]]
- [对抗:: [[Concept_Responsibility_Black_Hole.md]]]

[^1]: [[Source_第九讲：临床路径与闭环管理：医嘱、护理、手术麻醉、重症监护系统.md]]
