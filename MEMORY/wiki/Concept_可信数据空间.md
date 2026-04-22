---
id: "20260422_c009_v2"
title: "Concept: 可信数据空间 (Trusted Data Space / TDS)"
type: "concept"
domain: "Medical_IT"
topic_cluster: "Data_Governance"
status: "Active"
alignment_score: 98
epistemic-status: "evergreen"
ttl: 1825
categories: ["Healthcare_IT", "System_Architecture"]
tags: ["数据要素", "隐私计算", "联邦协作", "高质量数据集"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/digitalhealthobserve/高质量医疗数据集建设：现状、挑战与未来路径.md"]
---

# Concept: 可信数据空间 (TDS)

## 定义
医疗机构可信数据空间 (TDS) 是打破医疗数据孤岛、实现“可用不可见”安全流转的战略基础设施。

## 核心机制
- **数据不动模型动**: 允许算法在受控的“沙盒”环境中消费数据，而原始数据不出域。
- **语义主权锁定**: 通过集成 [[Entity_OMOP_CDM.md]] 确保多方协作时的语义对齐[^1]。
- **存算分离与审计**: 配合 [[Concept_Evidence_Mesh.md]] 实现全过程的计算审计。

## 战略价值
- **高质量数据集的孵化器**: 解决了数据囤积与数据共享之间的博弈。
- **对冲合规负债**: 将隐私泄露风险降至最低。
- **支撑 Agentic Hospital**: 为分布式智能体提供可信的知识输入[^1]。

## 关联
- [支持:: [[Concept_Data_as_a_Product]]]
- [属于:: [[Strategy_十五五公立医院数字化双轨制生存]]]
- [衍生于:: [[Source_高质量医疗数据集建设：现状、挑战与未来路径.md]]]

[^1]: [[Source_高质量医疗数据集建设：现状、挑战与未来路径.md]]
