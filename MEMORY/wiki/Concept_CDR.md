---
id: "20260422_c020"
title: "Concept: CDR"
type: "concept"
domain: "Healthcare_IT"
topic_cluster: "Clinical_Data"
status: "Active"
alignment_score: 95
epistemic-status: "sprouting"
ttl: 1825
categories: ["Healthcare_IT"]
tags: ["临床数据中心", "数据集成", "EMR-Centric"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗数字化三十讲/第六讲：心脏与大脑（上）：以电子病历（EMR）为核心的临床数据体系.md"]
---

# Concept: CDR (临床数据中心)

## 核心内涵
CDR (Clinical Data Repository) 是汇聚、清洗并整合医院内各异构系统（EMR, PACS, LIS, HIS 等）产生的临床数据的物理与逻辑实体。它旨在提供一个以“患者”为核心的、跨时间跨学科的全局临床视图[^1]。

## 物理地位
在 [包含:: [[Entity_WiNEX.md]]] 等现代架构中，CDR 充当了“临床操作系统”的数据内核，使上层应用不再受限于底层的烟囱式数据库[^1]。

## 关键价值
- **数据治理底座**：是 [包含:: [[Concept_数据治理.md]]] 在医院落地的核心对象。
- **决策支持基础**：为 CDSS（临床决策支持系统）提供多维度的实时输入 [支撑:: [[Concept_MSL_医疗语义层.md]]][^1]。

## 关联关系
- [属于:: [[Concept_四大架构.md]]] (数据架构部分)
- [支持:: [[Concept_MSL_医疗语义层.md]]]
- [衍生于:: [[Source_第六讲：心脏与大脑（上）：以电子病历（EMR）为核心的临床数据体系.md]]]

[^1]: [[Source_第六讲：心脏与大脑（上）：以电子病历（EMR）为核心的临床数据体系.md]]
