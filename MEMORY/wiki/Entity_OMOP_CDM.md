---
id: "20260422_ent_omop"
title: "OMOP CDM"
type: "entity"
domain: "System_Architecture"
topic_cluster: "General"
status: "Active"
alignment_score: 97
epistemic-status: "evergreen"
ttl: 1095
categories: ["System_Architecture"]
tags: ["通用数据模型", "医疗大数据", "OHDSI"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗数字化三十讲/第二十讲：大数据：从“杂乱无章”到“价值连城”的点金术.md"]
---

# OMOP CDM (Observational Medical Outcomes Partnership Common Data Model)

## 定义
OMOP CDM 是由 **[[Entity_OHDSI]]** 组织维护的全球通用的观测性医疗数据模型。它旨在通过将不同来源、不同结构的医疗数据（如 EMR、理赔数据）映射到统一的语义和结构上，从而实现跨机构的协同研究[^1]。

## 核心价值
- **车同轨，书同文**：打破 **[[Concept_Information_Silos.md]]**，实现大规模协作。
- **支撑 RWE**：是生成高信度 **[[Concept_RWE]]** 的物理底座。
- **算法迁移**：在 A 医院训练的模型可以快速部署到遵循 OMOP 标准的 B 医院。

## 技术特性
- 强制性的术语对齐（Mapping to Standard Vocabularies）。
- 强调“以患者为中心”的表结构设计。

[^1]: [[Source_第二十讲：大数据：从“杂乱无章”到“价值连城”的点金术.md]]
