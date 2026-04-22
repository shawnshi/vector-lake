---
id: "20260422_e001cpo"
title: "Entity_CPOE (医嘱处理系统)"
type: "entity"
domain: "Medical_IT"
topic_cluster: "Clinical_System"
status: "Active"
alignment_score: 100
epistemic-status: "evergreen"
categories: ["Healthcare_IT"]
tags: ["CPOE", "医嘱", "创世指令", "医疗安全"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗数字化三十讲/第九讲：临床路径与闭环管理：医嘱、护理、手术麻醉、重症监护系统.md"]
---

# Entity: 医嘱处理系统 (CPOE)

## 定义
CPOE (Computerized Physician Order Entry) 是临床医疗流程的起点。它被定义为医疗世界的**“创世指令”**发起端，是数字化介入医疗安全的第一道物理防线[^1]。

## 核心能力
- **规则引擎**: 实时进行配伍禁忌、重复开药、过敏提醒的拦截。
- **语义解析**: 将医生的模糊意图转化为可执行的标准化指令。
- **闭环源头**: 开启 **[[Concept_Closed_Loop_Management.md]]** 的第一跳。

## 战略地位
CPOE 是医生与系统交互最高频的节点，也是捕获 **[[Concept_Flow_Process_Data.md]]** 的最核心入口[^1]。

## 关联节点
- [属于:: [[Entity_CIS.md]]]
- [支持:: [[Concept_MSL_医疗语义层.md]]]

[^1]: [[Source_第九讲：临床路径与闭环管理：医嘱、护理、手术麻醉、重症监护系统.md]]
