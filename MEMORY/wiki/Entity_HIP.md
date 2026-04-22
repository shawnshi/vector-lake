---
id: "20260422_e001hip"
title: "Entity_HIP (医院信息集成平台)"
type: "entity"
domain: "Medical_IT"
topic_cluster: "Integration"
status: "Active"
alignment_score: 100
epistemic-status: "evergreen"
categories: ["Healthcare_IT", "System_Architecture"]
tags: ["HIP", "集成平台", "中枢神经系统", "互联互通"]
created: "2026-04-22"
updated: "2026-04-22"
sources: ["raw/article/医疗数字化三十讲/第七讲：心脏与大脑（下）：医院信息集成平台（HIP）与数据互联互通.md"]
---

# Entity: 医院信息集成平台 (HIP)

## 定义
医院信息集成平台（Hospital Integration Platform）是现代医院的“中央神经系统”。它通过标准化的数据交换协议（如 HL7/FHIR），将孤立的子系统（HIS, LIS, PACS 等）连接起来，实现跨系统的业务协同与数据共享[^1]。

## 核心功能
- **消息交换**: 通过 **[[Entity_ESB.md]]** 实现数据的有序流转。
- **患者主索引 (EMPI)**: 确保同一患者在不同系统间的身份唯一识别。
- **数据治理**: 充当 **[[Medical Semantic Layer (MSL)]]** 的物理载体。
- **业务协同**: 驱动跨科室、跨院区的复杂临床闭环。

## 战略价值
HIP 的建设是医院从“单一软件应用”向“平台化治理”跨越的标志。它解决了 **[[Concept_Spaghetti_Architecture.md]]** 带来的脆弱性，是构建“预见性医院”的底座[^1]。

## 关联节点
- [支持:: [[Entity_ESB.md]]]
- [属于:: [[Concept_MSL_医疗语义层.md]]]
- [对抗:: [[Concept_Information_Silos.md]]]

[^1]: [[Source_第七讲：心脏与大脑（下）：医院信息集成平台（HIP）与数据互联互通.md]]
