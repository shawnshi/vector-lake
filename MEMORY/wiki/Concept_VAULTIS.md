---
id: "20260422_c013vaultis"
title: "Concept: VAULTIS 准则 (VAULTIS Principles)"
type: "concept"
domain: "Medical_IT"
topic_cluster: "Data_Strategy"
status: "Active"
alignment_score: 95
epistemic-status: "evergreen"
ttl: 1825
categories: ["Strategy_and_Business", "Healthcare_IT"]
tags: ["DoD", "数据质量", "战略资产", "高质量数据集"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/digitalhealthobserve/高质量医疗数据集建设：现状、挑战与未来路径.md"]
---

# Concept: VAULTIS 准则

## 定义
VAULTIS 是由 [[Entity_DoD.md]] 提出的一套数据治理核心准则，旨在将数据转化为可用的“战略资产”。在医疗 AI 领域，它是衡量“高质量数据集”的物理金标准[^1]。

## 核心维度 (VAULTIS)
- **Visible (可见)**: 数据能够被潜在用户发现和定位。
- **Accessible (可及)**: 获得授权的用户能够通过物理接口获取数据。
- **Understandable (可理解)**: 具备清晰的元数据、语义定义与背景描述。
- **Linked (可关联)**: 不同的数据集之间能够通过唯一标识进行交叉关联。
- **Trustworthy (可信)**: 数据的来源、血缘 (Lineage) 与修改历史清晰可查。
- **Interoperable (可互操作)**: 采用标准格式（如 [[Entity_OMOP_CDM.md]]）实现语义对齐。
- **Secure (安全)**: 具备完善的脱敏、加密与权限管控机制[^1]。

## 战略意义
- **对抗数据孤岛**: 为 [Concept_可信数据空间] 提供了统一的验收契约。
- **数据即产品**: 是实现 [Concept_Data_as_a_Product] 的工程目标。

[^1]: [[Source_高质量医疗数据集建设：现状、挑战与未来路径.md]]
