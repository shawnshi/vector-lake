---
id: "20260422_c001hl7"
title: "Concept: HL7 & FHIR (医疗信息传输标准)"
type: "concept"
domain: "Medical_IT"
topic_cluster: "Integration"
status: "Active"
alignment_score: 100
epistemic-status: "evergreen"
categories: ["System_Architecture", "Healthcare_IT"]
tags: ["HL7", "FHIR", "标准规约", "互联互通"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗数字化三十讲/第七讲：心脏与大脑（下）：医院信息集成平台（HIP）与数据互联互通.md"]
---

# Concept: HL7 & FHIR (医疗信息传输标准)

## 定义
医疗信息交换的全球通行准则。它们定义了不同系统之间如何对话。

## 演进逻辑
- **HL7 v2 (医疗 IT 的“普通话”)**: 基于电报码逻辑的管道分隔符（|）格式。由于其高度灵活性，也导致了严重的“标准内碎片化”。
- **FHIR (医疗 IT 的“世界语”)**: 面向资源（Resource）的 RESTful 架构。被隐喻为“乐高积木”，是实现 **[[Medical Semantic Layer (MSL)]]** 的物理语义基础[^1]。

## 战略意义
标准是消除 **[[Concept_Information_Silos.md]]** 的唯一手段。掌握 FHIR 的应用能力意味着具备了构建 **[[Concept_Medical_P&L_Engine.md]]** 的数据解释权。

## 关联节点
- [支持:: [[Entity_HIP.md]]]
- [属于:: [[Concept_MSL_医疗语义层.md]]]

[^1]: [[Source_第七讲：心脏与大脑（下）：医院信息集成平台（HIP）与数据互联互通.md]]
