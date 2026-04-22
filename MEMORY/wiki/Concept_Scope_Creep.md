---
id: "20260422_sc099"
title: "Concept: 范围蠕变 (Scope Creep)"
type: "concept"
domain: "Medical_IT"
topic_cluster: "Governance"
status: "Active"
alignment_score: 100
epistemic-status: "evergreen"
ttl: 1825
categories: ["Strategy_and_Business"]
tags: ["Project_Management", "Delivery", "Debt", "Governance"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗数字化三十讲/第二十七讲：项目管理与交付：确保蓝图不只是“墙上的画”.md"]
---

# 范围蠕变 (Scope Creep)

## 定义
在项目管理中，指由于缺乏严格的控制、定义不明确或客户需求不断增加，导致项目范围在未经正式变更流程的情况下逐渐扩大的现象[^1]。

## 医疗 IT 中的表现 [属于:: [[Concept_Delivery_Debt.md]]]
- **“顺手做个小功能”**: 医生在病房走廊随口提的需求，被开发人员私下答应。
- **过度个性化**: 试图通过软件满足每个主任不同的操作习惯，导致系统代码臃肿不堪 [关联:: [[Concept_Liquid_Code.md]]]。
- **需求无底洞**: 医院领导在项目后期突然提出全新的战略模块。

## 后果
- **交付延期**: 消耗了大量计划外的开发时间[^1]。
- **质量崩塌**: 为了赶进度而牺牲代码质量，产生 [[Concept_Spaghetti_Architecture.md]]。
- **资源透支**: 导致项目组成员疲于奔命，陷入 [[Concept_Time_Black_Hole.md]]。

## 治理机制
- **建立基线 (Baseline)**: 明确初始合同范围。
- **CCB 委员会**: 需求变动必须经过“变更控制委员会”的商业与技术评估[^1]。
- **付费变更**: 明确范围外需求需额外支付成本，利用“经济杠杆”抑制无效需求。

[^1]: [[Source_第二十七讲：项目管理与交付：确保蓝图不只是“墙上的画”.md]]
