---
id: "20260422_c006"
title: "Concept: 算力房东模式 (Compute Landlord)"
type: "concept"
domain: "Strategy_and_Business"
topic_cluster: "General"
status: "Active"
alignment_score: 90
epistemic-status: "seed"
ttl: 1825
categories: ["Strategy_and_Business"]
tags: ["云化战略", "Oracle", "Microsoft", "数据租金"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/HealthcareIndustryRadar/DHWB-Radar-20260322.md"]
---

# Concept: 算力房东模式 (Compute Landlord)

## 定义
描述了云巨头与医疗 IT 巨头通过强制云原生化与提供不可替代的底座模型，将传统的“软件售卖/维护”模式，转变为长期的“算力流量费”与“数据托管租金”收取模式。

## 产生特征
1. **去应用化**: 厂商不再致力于开发复杂的业务应用逻辑，而是将精力集中在构建极其稳健、带合规断路器的云算力平台。
2. **强制锁定**: 通过将核心业务逻辑与特定的云底座（如 OCI, Azure Health）深度耦合，使医院在物理上无法迁移，从而实现长期的“算力收税”[^1]。
3. **数据租金**: 医院在处理、分析、训练自身数据时，必须支付高昂的模型调用费与算力费。

## 典型案例
- [支持:: [[Entity_Oracle_Health.md]]] 全面转向 OCI 云底座，剥离传统 HIS 交付，即为典型的“算力房东”转型。

## 防御策略
- **本地化一体机**: 如 [[Entity_B-Soft.md]] 的“慧康·云枢”，通过本地算力物理墙对抗云端的算力垄断。
- **语义主权**: 通过 [[Concept_MSL_医疗语义层.md]] 实现逻辑对底层算力的解耦。

[^1]: [[Source_DHWB-Radar-20260322.md]]
