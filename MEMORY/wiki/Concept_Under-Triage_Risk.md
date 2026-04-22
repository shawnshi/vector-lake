---
id: "20260422_c00101"
title: "Concept: 致死性漏诊风险 (Under-Triage Risk)"
type: "concept"
domain: "Medical_IT"
topic_cluster: "Safety_and_Risk"
status: "Active"
alignment_score: 100
epistemic-status: "sprouting"
ttl: 1825
categories: ["Artificial_Intelligence"]
tags: ["Patient_Safety", "ED_Triage", "Hallucination"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/DigitalHealthLecturesScout/Weekly_DigitalHealth_20260308.md"]
---

# 致死性漏诊风险 (Under-Triage Risk)

## 定义
指医疗人工智能系统（特别是基于通用 LLM 的分诊工具）在处理危急重症时，由于概率偏差、逻辑塌缩或 UI/UX 引导错误，将高风险患者误判为低风险状态，从而导致延误治疗甚至致死的情况。

## 核心特征
- **概率偏差**: 模型在处理“长尾”极端病例时，倾向于向均值回归，忽略了极低频但致命的症状 [^1]。
- **意图解析失败**: 医生在急诊高压环境下输入的非结构化文本可能无法触发模型的危急值警报 [^1]。
- **UI 诱导**: 过度简洁的界面设计可能导致关键阴性体征被掩盖。

## 缓解策略
- **[[Concept_Cognitive_Friction]]**: 强制引入逻辑减震器。
- **[[Concept_MSL_医疗语义层]]**: 建立确定性的医学规则校验网。

## 关联节点
- [反驳:: [[Concept_Zero_Tolerance_Threshold]]]
- [对比:: [[Concept_Automation_Bias]]]

[^1]: [[Source_Weekly_DigitalHealth_20260308.md]]
